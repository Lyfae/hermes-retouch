"""Keyless adjustment ops. Each takes the whole crop and returns an edited crop of the same size;
``composite.apply_in_region`` confines the result to the mark. Alpha is preserved."""

from __future__ import annotations

from typing import Callable

from PIL import Image, ImageColor, ImageEnhance, ImageFilter, ImageOps

Op = Callable[[Image.Image, float | str], Image.Image]


def _rgb_alpha(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    return img.convert("RGB"), alpha


def _restore(rgb: Image.Image, alpha: Image.Image | None) -> Image.Image:
    if alpha is not None:
        rgb = rgb.convert("RGBA")
        rgb.putalpha(alpha)
    return rgb


def _enhancer(cls) -> Op:
    def op(img, value):
        rgb, alpha = _rgb_alpha(img)
        return _restore(cls(rgb).enhance(1.0 + float(value)), alpha)
    return op


def _exposure(img, value):
    rgb, alpha = _rgb_alpha(img)
    return _restore(ImageEnhance.Brightness(rgb).enhance(2.0 ** float(value)), alpha)


def _warmth(img, value):
    rgb, alpha = _rgb_alpha(img)
    v = float(value)
    r, g, b = rgb.split()
    r = r.point(lambda p: max(0, min(255, round(p * (1 + 0.25 * v)))))
    b = b.point(lambda p: max(0, min(255, round(p * (1 - 0.25 * v)))))
    return _restore(Image.merge("RGB", (r, g, b)), alpha)


def _sharpen(img, value):
    rgb, alpha = _rgb_alpha(img)
    return _restore(rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=round(150 * float(value)), threshold=2)), alpha)


def _blur(img, value):
    rgb, alpha = _rgb_alpha(img)
    return _restore(rgb.filter(ImageFilter.GaussianBlur(float(value))), alpha)


def _denoise(img, value):
    rgb, alpha = _rgb_alpha(img)
    size = 3 if float(value) < 0.5 else 5
    return _restore(Image.blend(rgb, rgb.filter(ImageFilter.MedianFilter(size)), min(1.0, 0.5 + float(value) / 2)), alpha)


def _pixelate(img, value):
    rgb, alpha = _rgb_alpha(img)
    block = max(2, int(value))
    small = rgb.resize((max(1, rgb.width // block), max(1, rgb.height // block)), Image.Resampling.BOX)
    return _restore(small.resize(rgb.size, Image.Resampling.NEAREST), alpha)


def _redact(img, value):
    color = ImageColor.getrgb(str(value or "black"))
    rgb, alpha = _rgb_alpha(img)
    return _restore(Image.new("RGB", rgb.size, color[:3]), alpha)


def _grayscale(img, value):
    rgb, alpha = _rgb_alpha(img)
    gray = ImageOps.grayscale(rgb).convert("RGB")
    return _restore(Image.blend(rgb, gray, max(0.0, min(1.0, float(value)))), alpha)


# name: (function, (min, max) for numeric values or None for strings, default, what it does)
OPS: dict[str, tuple[Op, tuple[float, float] | None, float | str, str]] = {
    "exposure": (_exposure, (-3.0, 3.0), 0.5, "EV stops; +1 doubles brightness"),
    "brightness": (_enhancer(ImageEnhance.Brightness), (-1.0, 1.0), 0.2, "-1..1"),
    "contrast": (_enhancer(ImageEnhance.Contrast), (-1.0, 1.0), 0.2, "-1..1"),
    "saturation": (_enhancer(ImageEnhance.Color), (-1.0, 1.0), 0.2, "-1..1; -1 is grayscale"),
    "warmth": (_warmth, (-1.0, 1.0), 0.2, "-1 cooler .. +1 warmer"),
    "sharpen": (_sharpen, (0.0, 3.0), 1.0, "unsharp mask strength"),
    "blur": (_blur, (0.0, 60.0), 6.0, "gaussian radius in pixels"),
    "denoise": (_denoise, (0.0, 1.0), 0.5, "median-filter strength"),
    "pixelate": (_pixelate, (2.0, 200.0), 16.0, "block size in pixels"),
    "redact": (_redact, None, "black", "fill with a solid color (name or #hex)"),
    "grayscale": (_grayscale, (0.0, 1.0), 1.0, "0..1 blend toward gray"),
}


class OpError(ValueError):
    pass


# Tonal ops fade out toward the mark's edge (like a graduated filter) so no outline shows. Privacy ops
# cover every pixel of the mark: a redacted face must not show through a faded border.
SOFT_EDGE_OPS = frozenset({"exposure", "brightness", "contrast", "saturation", "warmth", "grayscale", "sharpen", "denoise"})
PRIVACY_OPS = frozenset({"redact", "pixelate", "blur"})


def edge_for(ops: list[tuple[str, float | str]]) -> str:
    """'hard' if any privacy op is present, 'soft' if every op is tonal, else 'default'."""
    names = {name for name, _ in ops}
    if names & PRIVACY_OPS:
        return "hard"
    return "soft" if names <= SOFT_EDGE_OPS else "default"


def normalize_ops(ops: list) -> list[tuple[str, float | str]]:
    if not isinstance(ops, list) or not ops:
        raise OpError("ops must be a non-empty list of {op, value}")
    clean = []
    for i, item in enumerate(ops):
        if not isinstance(item, dict) or item.get("op") not in OPS:
            raise OpError(f"ops[{i}].op must be one of {', '.join(OPS)}")
        name = item["op"]
        _fn, bounds, default, _doc = OPS[name]
        value = item.get("value", default)
        if bounds is None:
            try:
                ImageColor.getrgb(str(value))
            except ValueError:
                raise OpError(f"ops[{i}].value must be a color name or #hex") from None
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise OpError(f"ops[{i}].value must be a number") from None
            value = max(bounds[0], min(bounds[1], value))
        clean.append((name, value))
    return clean


def run_ops(crop: Image.Image, ops: list[tuple[str, float | str]]) -> Image.Image:
    for name, value in ops:
        crop = OPS[name][0](crop, value)
    return crop


def describe() -> str:
    return "; ".join(f"{n} ({doc})" for n, (_f, _b, _d, doc) in OPS.items())
