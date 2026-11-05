"""The edge check: a generated removal that wanted to change pixels past the mark is reported, and the
agent cannot hand such a mark back for review while saying nothing remains."""

import io

import pytest
from PIL import Image, ImageChops, ImageDraw

from retouch import composite, service

GREY = (100, 100, 100)
MARK = {"x": 200, "y": 150, "w": 240, "h": 160}


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def remove(ws, img, generator, shape="ellipse"):
    proj = ws.create_project("scene.png", png_bytes(img))
    n = ws.add_pin(proj["id"], shape=shape, geom=MARK, intent="remove", note="remove the bag")["number"]
    return proj["id"], n, ws.edit(proj["id"], n, method="generate", generator=generator)


def clean_grey(path, prompt):
    return Image.new("RGB", (1024, 1024), GREY)


def test_an_object_crossing_the_outline_is_flagged_on_that_side(ws):
    img = Image.new("RGB", (640, 480), GREY)
    ImageDraw.Draw(img).rectangle((150, 215, 330, 245), fill="white")  # a handle poking out on the left
    pid, n, result = remove(ws, img, clean_grey)
    assert result.spill == ("left",)
    assert ws.project(pid)["versions"][-1]["spill"] == "left"


def test_an_object_inside_the_outline_is_not_flagged(ws):
    img = Image.new("RGB", (640, 480), GREY)
    ImageDraw.Draw(img).ellipse((250, 180, 390, 280), fill="white")
    _, _, result = remove(ws, img, clean_grey)
    assert result.spill == ()


def test_a_redrawn_texture_shifted_by_a_pixel_is_not_flagged(ws):
    img = Image.new("RGB", (640, 480), (150, 140, 125))
    draw = ImageDraw.Draw(img)
    for x in range(0, 640, 23):
        draw.line((x, 0, x, 480), fill=(60, 55, 50), width=2)
    for y in range(0, 480, 17):
        draw.line((0, y, 640, y), fill=(60, 55, 50), width=2)

    def redrawn(path, prompt):  # same pavement, re-rendered one pixel off
        return ImageChops.offset(Image.open(path).convert("RGB"), 1, 1)

    _, _, result = remove(ws, img, redrawn, shape="rect")
    assert result.spill == ()


def test_a_flagged_mark_cannot_go_back_for_review_with_nothing_reported(ws):
    img = Image.new("RGB", (640, 480), GREY)
    ImageDraw.Draw(img).rectangle((150, 215, 330, 245), fill="white")
    pid, n, result = remove(ws, img, clean_grey)

    with pytest.raises(service.RetouchError, match="left"):
        ws.reply(pid, n, "Removed the bag cleanly.", "needs_review", result.version_id, issues="")
    assert ws.project(pid)["pins"][0]["status"] == "working"

    pin = ws.reply(pid, n, "Removed the bag.", "needs_review", result.version_id,
                   issues="the end of the handle outside the mark's left edge remains")
    assert pin["status"] == "needs_review"


def test_the_models_raw_output_is_kept_for_review(ws):
    img = Image.new("RGB", (640, 480), GREY)
    ImageDraw.Draw(img).ellipse((250, 180, 390, 280), fill="white")
    pid, _, _ = remove(ws, img, clean_grey)
    outs = list((ws.project_dir(pid) / "crops").glob("*-out.png"))
    assert len(outs) == 1 and Image.open(outs[0]).size == (1024, 1024)


def test_spill_text_reads_naturally():
    assert service.spill_text(["left"]) == "left"
    assert service.spill_text(["bottom", "bottom-left", "top-left"]) == "bottom, bottom-left and top-left"


def test_spill_sides_refuses_mismatched_sizes():
    with pytest.raises(ValueError):
        composite.spill_sides(Image.new("RGB", (10, 10)), Image.new("RGB", (12, 10)), Image.new("L", (10, 10)))
