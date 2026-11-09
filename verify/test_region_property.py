"""Property check: an edit never changes a pixel outside its mark, for EVERY image, mark and edit
Hypothesis can generate within the bounds below.

The promise (retouch/composite.py: apply_in_region, blend_full): the result equals the original on
every pixel where the mark's mask is 0. The unit tests check hand-picked cases; this checks
generated ones: random image sizes and modes, every shape (including masks that touch the image
edge or extend past it), every adjust op at random strength, feathers from 0 to a quarter of the
image, replacements of random content and arbitrary edit_fn output sizes.

Run:  uv run --no-project --python 3.14 --with "pillow==13.0.0" --with pytest --with hypothesis \
        pytest verify -q
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import assume, event, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from retouch import composite, geometry, ops  # noqa: E402

SIZES = st.tuples(st.integers(24, 96), st.integers(24, 96))
MODES = st.sampled_from(["RGB", "RGBA", "L"])


def noise(size, seed, mode):
    rnd = random.Random(seed)
    n = size[0] * size[1]
    bands = len(mode)
    return Image.frombytes(mode, size, bytes(rnd.getrandbits(8) for _ in range(n * bands)))


@st.composite
def images(draw):
    return noise(draw(SIZES), draw(st.integers(0, 2**31)), draw(MODES))


unit = st.floats(-0.3, 1.3)  # a little past the image on purpose: marks may hang off the edge


@st.composite
def geoms(draw):
    shape = draw(st.sampled_from(geometry.SHAPES))
    if shape in ("ellipse", "rect"):
        return shape, {"x": draw(unit), "y": draw(unit), "w": draw(st.floats(0.02, 1.0)), "h": draw(st.floats(0.02, 1.0))}
    n = draw(st.integers(3 if shape == "lasso" else 1, 12))
    pts = [[draw(unit), draw(unit)] for _ in range(n)]
    if shape == "lasso":
        return shape, {"points": pts}
    return shape, {"points": pts, "width": draw(st.floats(0.01, 0.3))}


def mask_or_none(shape, geom, size):
    try:
        mask = geometry.build_mask(shape, geom, size)
    except geometry.GeometryError:
        event("mark covers no pixel (refused upstream)")
        return None  # a mark that covers no pixel is refused upstream; nothing to check
    event(f"shape={shape}")
    event("mark touches the image edge" if mask.getbbox() != (0, 0, *size) and (
        mask.getbbox()[0] == 0 or mask.getbbox()[1] == 0 or mask.getbbox()[2] == size[0] or mask.getbbox()[3] == size[1]
    ) else "mark inside the image")
    return mask


def outside_is_untouched(before, after, mask):
    """Exact comparison on every band, not the thresholded count the pipeline uses."""
    diff = ImageChops.difference(before.convert("RGBA"), after.convert("RGBA"))
    changed = Image.new("L", before.size, 0)
    for band in diff.split():  # colour and alpha
        changed = ImageChops.lighter(changed, band.point(lambda v: 255 if v else 0))
    outside = ImageChops.subtract(changed, mask)
    return outside.getbbox() is None


@st.composite
def adjust_ops(draw):
    name = draw(st.sampled_from([n for n, spec in ops.OPS.items() if spec[1] is not None]))
    lo, hi = ops.OPS[name][1]
    return [(name, draw(st.floats(lo, hi)))]


@settings(max_examples=150, deadline=None)
@given(img=images(), g=geoms(), edit=st.one_of(adjust_ops(), st.just([("redact", "#ff0000")])),
       feather=st.floats(0, 0.25), color_match=st.booleans(), square=st.booleans())
def test_adjust_in_region_leaves_every_pixel_outside_the_mark(img, g, edit, feather, color_match, square):
    shape, geom = g
    mask = mask_or_none(shape, geom, img.size)
    if mask is None:
        return
    clean = ops.normalize_ops([{"op": n, "value": v} for n, v in edit])
    out = composite.apply_in_region(img, mask, lambda c, s: ops.run_ops(c, clean),
                                    feather=feather * min(img.size), color_match=color_match, square=square)
    assert out.size == img.size and out.mode == img.mode
    assert outside_is_untouched(img, out, mask)


@settings(max_examples=100, deadline=None)
@given(img=images(), g=geoms(), seed=st.integers(0, 2**31), scale=st.floats(0.3, 2.5), feather=st.floats(0, 0.25))
def test_a_generated_fill_of_any_size_is_fitted_and_stays_inside(img, g, seed, scale, feather):
    """A model may return a different size than the crop it was given."""
    shape, geom = g
    mask = mask_or_none(shape, geom, img.size)
    if mask is None:
        return

    def gen(crop, _soft):
        w, h = max(1, round(crop.width * scale)), max(1, round(crop.height * scale * 1.1))
        return noise((w, h), seed, "RGB")

    report = {}
    out = composite.apply_in_region(img, mask, gen, feather=feather * min(img.size), color_match=True, square=True,
                                    report=report)
    assert outside_is_untouched(img, out, mask)
    assert set(report["spill"]) <= {"top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right"}


@settings(max_examples=100, deadline=None)
@given(img=images(), g=geoms(), seed=st.integers(0, 2**31), feather=st.floats(0, 0.25))
def test_from_file_takes_only_the_marked_area(img, g, seed, feather):
    shape, geom = g
    mask = mask_or_none(shape, geom, img.size)
    if mask is None:
        return
    replacement = noise(img.size, seed, "RGB")
    out = composite.blend_full(img, replacement, mask, feather=feather * min(img.size))
    assert outside_is_untouched(img, out, mask)
    assert composite.outside_changed_px(img, out, mask) == 0


@settings(max_examples=100, deadline=None)
@given(img=images(), g=geoms(), seed=st.integers(0, 2**31))
def test_the_pipelines_own_check_agrees_with_the_exact_comparison(img, g, seed):
    """outside_changed_px is what refuses a bad edit before it is saved: it must flag any change
    outside the mask that the exact comparison sees (it may use a tolerance only for noise-free
    encodings, so 0 here must mean exactly 0 changed pixels)."""
    shape, geom = g
    mask = mask_or_none(shape, geom, img.size)
    if mask is None:
        return
    tampered = img.copy()
    rnd = random.Random(seed)
    # change one pixel outside the mask: pick it from the mask's complement, never by blind sampling
    outside_pixels = [(i % img.width, i // img.width) for i, v in enumerate(ImageChops.invert(mask).getdata()) if v]
    assume(outside_pixels)  # a mark covering the whole image leaves nothing outside to tamper with
    x, y = outside_pixels[rnd.randrange(len(outside_pixels))]
    px = tampered.getpixel((x, y))
    new = tuple((c + 128) % 256 for c in px) if isinstance(px, tuple) else (px + 128) % 256
    tampered.putpixel((x, y), new)
    assert composite.outside_changed_px(img, tampered, mask) >= 1
