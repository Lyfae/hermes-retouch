"""Region-limited edits: crop around a mark, edit the crop, paste it back inside the mark only.

The guarantee this module exists for: no pixel outside the drawn mark ever changes.
``apply_in_region`` makes it true by construction (the blend mask is the inward-feathered mark,
multiplied by the hard mark), and ``outside_changed_px`` proves it after the fact.
"""

from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

EditFn = Callable[[Image.Image, Image.Image], Image.Image]


def default_feather(size: tuple[int, int]) -> float:
    return max(1.5, 0.004 * min(size))


def generate_feather(size: tuple[int, int]) -> float:
    """Generated fills already continue the surroundings; a wide fade would only let the original
    (the object being removed) ghost through at the mark's edge."""
    return max(1.0, 0.0015 * min(size))


def soft_feather(hard_mask: Image.Image, fraction: float = 0.25) -> float:
    """Feather for tonal adjustments: the fade reaches about ``fraction`` of the mark's shorter side
    (``inward_soft_mask`` erodes ~2 feathers and blurs ~1, so ~3 feathers of fade)."""
    bbox = hard_mask.getbbox()
    if bbox is None:
        raise ValueError("empty mask")
    return max(default_feather(hard_mask.size), fraction * min(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 3)


def _odd(n: float) -> int:
    n = max(1, int(round(n)))
    return n if n % 2 else n + 1


def inward_soft_mask(hard: Image.Image, feather: float) -> Image.Image:
    """Soft blend mask that is 0 everywhere ``hard`` is 0: erode, blur, then clip to ``hard``."""
    if feather <= 0:
        return hard
    eroded = hard.filter(ImageFilter.MinFilter(_odd(4 * feather)))
    if eroded.getbbox() is None:  # mark thinner than the feather: keep it hard rather than empty
        return hard
    return ImageChops.multiply(eroded.filter(ImageFilter.GaussianBlur(feather)), hard)


def region_box(hard_mask: Image.Image, pad: float = 0.15, square: bool = False) -> tuple[int, int, int, int]:
    """The mark's bounding box grown by ``pad`` of its size on each side, clamped to the image.
    ``square`` grows the shorter side too (generative models keep framing best on square input)."""
    bbox = hard_mask.getbbox()
    if bbox is None:
        raise ValueError("empty mask")
    x0, y0, x1, y1 = bbox
    px, py = max(2, int((x1 - x0) * pad)), max(2, int((y1 - y0) * pad))
    if square:
        side = max(x1 - x0 + 2 * px, y1 - y0 + 2 * py)
        px, py = (side - (x1 - x0)) // 2, (side - (y1 - y0)) // 2
    w, h = hard_mask.size
    return max(0, x0 - px), max(0, y0 - py), min(w, x1 + px), min(h, y1 + py)


def _ring(hard: Image.Image, width: int) -> Image.Image:
    """The band of untouched pixels just OUTSIDE the mark."""
    outer = hard.filter(ImageFilter.MaxFilter(_odd(2 * width + 1)))
    return ImageChops.subtract(outer, hard)


def match_ring_color(edited: Image.Image, reference: Image.Image, hard: Image.Image, width: int = 6) -> Image.Image:
    """Linear per-channel fit so ``edited`` matches ``reference`` on a thin ring just outside the mark.

    Generative models drift exposure and white balance over the whole crop they return; outside the
    mark both images show the same untouched surroundings, so that band measures the drift alone.
    (Inside the mark the reference is the very thing being removed.) Not used for adjustment ops.
    """
    ring = _ring(hard, width)
    if ring.getbbox() is None:
        return edited
    rgb_e, rgb_r = edited.convert("RGB"), reference.convert("RGB")
    se, sr = ImageStat.Stat(rgb_e, ring), ImageStat.Stat(rgb_r, ring)
    bands = []
    for band, me, sde, mr, sdr in zip(rgb_e.split(), se.mean, se.stddev, sr.mean, sr.stddev):
        gain = max(0.5, min(2.0, sdr / sde)) if sde > 1e-3 else 1.0
        bands.append(band.point(lambda v, g=gain, a=me, b=mr: max(0, min(255, round((v - a) * g + b)))))
    fitted = Image.merge("RGB", bands)
    if edited.mode == "RGBA":
        fitted.putalpha(edited.getchannel("A"))
    return fitted.convert(edited.mode)


def apply_in_region(
    original: Image.Image,
    hard_mask: Image.Image,
    edit_fn: EditFn,
    *,
    feather: float | None = None,
    pad: float = 0.15,
    color_match: bool = False,
    square: bool = False,
    report: dict | None = None,
) -> Image.Image:
    """Run ``edit_fn(crop, soft_mask)`` on the padded crop around the mark and blend the result back
    through the inward-feathered mark. The edit may return any size; it is fitted back to the crop.
    With ``report``, sets ``report["spill"]`` to the sides where the edit wanted to change the picture
    past the mark (meaningful for generated fills only: an adjustment changes the whole crop)."""
    if original.size != hard_mask.size:
        raise ValueError("mask size must match the image")
    feather = default_feather(original.size) if feather is None else feather
    box = region_box(hard_mask, pad, square)
    crop, hard = original.crop(box), hard_mask.crop(box)
    soft = inward_soft_mask(hard, feather)
    edited = edit_fn(crop.copy(), soft.copy()).convert(crop.mode)
    if edited.size != crop.size:
        same_aspect = abs(edited.width / edited.height - crop.width / crop.height) <= 0.03 * crop.width / crop.height
        edited = (edited.resize(crop.size, Image.Resampling.LANCZOS) if same_aspect
                  else ImageOps.fit(edited, crop.size, Image.Resampling.LANCZOS))
    if color_match:
        edited = match_ring_color(edited, crop, hard)
    if report is not None:
        report["spill"] = spill_sides(crop, edited, hard)
    out = original.copy()
    out.paste(Image.composite(edited, crop, soft), box[:2])
    return out


def blend_full(original: Image.Image, replacement: Image.Image, hard_mask: Image.Image,
               *, feather: float | None = None) -> Image.Image:
    """Take only the marked area from a full-size ``replacement`` (e.g. a version the agent computed
    itself) and keep ``original`` everywhere else."""
    if replacement.size != original.size or hard_mask.size != original.size:
        raise ValueError(f"the replacement must be {original.width}x{original.height}, the same size as the image")
    feather = default_feather(original.size) if feather is None else feather
    return Image.composite(replacement.convert(original.mode), original, inward_soft_mask(hard_mask, feather))


def _binary(img: Image.Image) -> Image.Image:
    return img.point(lambda v: 255 if v else 0)


def changed_mask(before: Image.Image, after: Image.Image) -> Image.Image:
    if before.size != after.size:
        raise ValueError("images must be the same size")
    diff = ImageChops.difference(before.convert("RGBA"), after.convert("RGBA"))
    r, g, b, a = diff.split()
    return _binary(ImageChops.lighter(ImageChops.lighter(r, g), ImageChops.lighter(b, a)))


def _count(binary: Image.Image) -> int:
    return sum(binary.histogram()[1:])


def outside_changed_px(before: Image.Image, after: Image.Image, hard_mask: Image.Image) -> int:
    """Number of pixels that differ between ``before`` and ``after`` outside the mark. Must be 0."""
    outside = ImageChops.invert(_binary(hard_mask))
    return _count(ImageChops.multiply(changed_mask(before, after), outside))


def inside_changed_px(before: Image.Image, after: Image.Image, hard_mask: Image.Image) -> int:
    return _count(ImageChops.multiply(changed_mask(before, after), _binary(hard_mask)))


SIDES = ("right", "bottom-right", "bottom", "bottom-left", "left", "top-left", "top", "top-right")


def spill_sides(original: Image.Image, edited: Image.Image, hard: Image.Image, *, threshold: int = 60) -> list[str]:
    """Sides of the mark where ``edited`` differs strongly from ``original`` just OUTSIDE the mark.

    A generative edit sees the whole padded crop and removes the whole object, so a strong change in
    the band around the mark means the object (or its shadow) continues past the outline. That part
    stays (nothing outside a mark ever changes): the user should know, and draw a bigger mark. Both
    images are blurred first so a re-rendered texture or a 1 px shift of an edge does not count.
    Calibrated on real xAI edits: a removed bag's handle tip (11 px) and shadow flag, a cleanly
    contained person removal stays at 0.
    """
    if original.size != edited.size or original.size != hard.size:
        raise ValueError("images and mask must be the same size")
    bbox = hard.getbbox()
    if bbox is None:
        return []
    width = max(4, round(0.06 * min(bbox[2] - bbox[0], bbox[3] - bbox[1])))
    ring = ImageChops.subtract(hard.filter(ImageFilter.MaxFilter(_odd(2 * width + 1))), hard)
    soften = ImageFilter.GaussianBlur(max(1.5, min(original.size) / 150))
    r, g, b = ImageChops.difference(original.convert("RGB").filter(soften), edited.convert("RGB").filter(soften)).split()
    strong = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > threshold else 0)
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    sx, sy = (bbox[2] - bbox[0]) / 2, (bbox[3] - bbox[1]) / 2
    hits = [0] * len(SIDES)
    flagged = ImageChops.multiply(_binary(ring), strong).tobytes()
    for i in (i for i, v in enumerate(flagged) if v):
        y, x = divmod(i, hard.width)
        angle = math.degrees(math.atan2((y - cy) / sy, (x - cx) / sx)) % 360
        hits[int(((angle + 22.5) % 360) // 45)] += 1
    min_px = max(8, round(0.002 * _count(_binary(ring))))
    return [SIDES[i] for i in range(len(SIDES)) if hits[i] >= min_px]
