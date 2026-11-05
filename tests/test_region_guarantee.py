"""L1: an edit may only change pixels inside the mark the user drew."""

import random

import pytest
from PIL import ImageChops

from conftest import noise
from retouch import composite, geometry, ops

SHAPES = [
    ("ellipse", {"x": 0.2, "y": 0.25, "w": 0.4, "h": 0.3}),
    ("rect", {"x": 0.55, "y": 0.1, "w": 0.3, "h": 0.5}),
    ("lasso", {"points": [[0.1, 0.6], [0.4, 0.55], [0.35, 0.95], [0.05, 0.9]]}),
    ("brush", {"points": [[0.1, 0.1], [0.3, 0.2], [0.6, 0.15]], "width": 0.05}),
    ("ellipse", {"x": 0.9, "y": 0.9, "w": 0.3, "h": 0.3}),  # partly off the image
    ("brush", {"points": [[0.5, 0.5]], "width": 0.004}),  # a dot thinner than the feather
]


def generate_stub(crop, soft):
    """A generative model: returns a different-sized, different-colored image."""
    return noise((crop.width * 2 + 37, crop.height + 11), seed=crop.width).convert(crop.mode)


EDITS = [(f"op:{name}", lambda c, s, n=name: ops.run_ops(c, ops.normalize_ops([{"op": n}])), False)
         for name in ops.OPS] + [("generate", generate_stub, True)]


@pytest.mark.parametrize("shape,geom", SHAPES)
@pytest.mark.parametrize("label,edit,color_match", EDITS, ids=[e[0] for e in EDITS])
@pytest.mark.parametrize("fixture", ["photo", "rgba_photo"])
def test_outside_pixels_never_change(request, fixture, shape, geom, label, edit, color_match):
    img = request.getfixturevalue(fixture)
    mask = geometry.build_mask(shape, geom, img.size)
    after = composite.apply_in_region(img, mask, edit, color_match=color_match)
    assert after.size == img.size and after.mode == img.mode
    assert composite.outside_changed_px(img, after, mask) == 0


def test_generative_edit_changes_the_inside(photo):
    mask = geometry.build_mask("ellipse", {"x": 0.2, "y": 0.2, "w": 0.5, "h": 0.5}, photo.size)
    after = composite.apply_in_region(photo, mask, generate_stub, color_match=True)
    assert composite.inside_changed_px(photo, after, mask) > 0.5 * sum(mask.histogram()[255:])


def test_inward_feather_stays_inside_shape():
    for seed in range(20):
        rnd = random.Random(seed)
        geom = {"x": rnd.random() * 0.6, "y": rnd.random() * 0.6, "w": 0.1 + rnd.random() * 0.4, "h": 0.1 + rnd.random() * 0.4}
        hard = geometry.build_mask("ellipse", geom, (160, 120))
        soft = composite.inward_soft_mask(hard, feather=1 + rnd.random() * 6)
        assert ImageChops.multiply(soft, ImageChops.invert(hard)).getbbox() is None
        assert soft.getbbox() is not None


def test_outside_proof_detects_a_leak(photo):
    mask = geometry.build_mask("rect", {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, photo.size)
    leaked = photo.copy()
    leaked.putpixel((photo.width - 1, photo.height - 1), (0, 0, 0))
    assert composite.outside_changed_px(photo, leaked, mask) == 1
