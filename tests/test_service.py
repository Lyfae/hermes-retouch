"""The edit pipeline end to end on a temp workspace (no Hermes, no network)."""

import io

import pytest
from PIL import Image

from conftest import noise
from retouch import composite, service, store


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def proj(ws):
    return ws.create_project("Trip Photo.png", png_bytes(noise((320, 240), seed=5)))


def mark(ws, proj, **kw):
    return ws.add_pin(proj["id"], shape="ellipse", geom={"x": 100, "y": 60, "w": 80, "h": 60}, note="brighten the face", **kw)


def outside_unchanged(ws, proj, pin_number):
    before, _ = ws.load(proj["id"], proj["versions"][0]["id"])
    after, _ = ws.load(proj["id"])
    pin = next(p for p in ws.project(proj["id"])["pins"] if p["number"] == pin_number)
    return composite.outside_changed_px(before, after, ws.mask(pin, before.size)) == 0


def test_adjust_edit_saves_a_proved_version(ws, proj):
    n = mark(ws, proj)["number"]
    result = ws.edit(proj["id"], n, method="adjust", ops=[{"op": "exposure", "value": 1}])
    assert result.outside_changed_px == 0 and result.inside_changed_px > 0
    now = ws.project(proj["id"])
    assert now["current_version"] == result.version_id
    assert now["pins"][0]["status"] == "working" and now["pins"][0]["result_version"] == result.version_id
    assert outside_unchanged(ws, proj, n)


def test_generate_edit_uses_the_generator_once_and_stays_inside(ws, proj):
    n = mark(ws, proj, intent="remove")["number"]
    calls = []

    def fake_generator(path, prompt):
        calls.append((path, prompt))
        crop = Image.open(path)
        return noise((1024, 1024), seed=9).convert(crop.mode)

    ws.edit(proj["id"], n, method="generate", generator=fake_generator)
    assert len(calls) == 1 and calls[0][0].is_file() and "brighten the face" in calls[0][1]
    assert outside_unchanged(ws, proj, n)


def test_failed_generation_puts_the_mark_back(ws, proj):
    n = mark(ws, proj)["number"]
    ws.user_action(proj["id"], n, "send")

    def broken(path, prompt):
        raise service.RetouchError("provider is down")

    with pytest.raises(service.RetouchError):
        ws.edit(proj["id"], n, method="generate", generator=broken)
    after = ws.project(proj["id"])
    assert after["pins"][0]["status"] == "queued" and len(after["versions"]) == 1


def test_bad_ops_are_refused_before_anything_changes(ws, proj):
    n = mark(ws, proj)["number"]
    with pytest.raises(service.RetouchError):
        ws.edit(proj["id"], n, method="adjust", ops=[{"op": "explode"}])
    assert ws.project(proj["id"])["pins"][0]["status"] == "open"


def test_generate_without_a_provider_is_a_clear_error(ws, proj):
    n = mark(ws, proj)["number"]
    with pytest.raises(service.RetouchError, match="image generation provider"):
        ws.edit(proj["id"], n, method="generate", generator=None)


def test_from_file_takes_only_the_marked_area(ws, proj):
    n = mark(ws, proj)["number"]
    other = ws.work_dir(proj["id"]) / "agent_full_edit.png"
    Image.new("RGB", (320, 240), (0, 0, 0)).save(other)
    ws.edit(proj["id"], n, method="from_file", file=str(other))
    assert outside_unchanged(ws, proj, n)


def test_from_file_of_the_wrong_size_is_refused(ws, proj):
    n = mark(ws, proj)["number"]
    other = ws.work_dir(proj["id"]) / "small.png"
    Image.new("RGB", (10, 10)).save(other)
    with pytest.raises(service.RetouchError, match="same size"):
        ws.edit(proj["id"], n, method="from_file", file=str(other))


def test_agent_reply_cannot_resolve(ws, proj):
    n = mark(ws, proj)["number"]
    with pytest.raises(store.StoreError):
        ws.reply(proj["id"], n, "done!", "resolved")
    pin = ws.reply(proj["id"], n, "Brightened by one stop.", "needs_review")
    assert pin["status"] == "needs_review" and pin["thread"][-1]["author"] == "agent"


def test_export_writes_new_files_and_leaves_versions_alone(ws, proj):
    n = mark(ws, proj)["number"]
    ws.edit(proj["id"], n, method="adjust", ops=[{"op": "contrast", "value": 0.5}])
    a = ws.export(proj["id"], fmt="jpeg")
    b = ws.export(proj["id"], fmt="jpeg")
    assert a != b and a.name == "Trip-Photo-retouched.jpg" and Image.open(a).size == (320, 240)
    assert a.parent == b.parent == ws.project_dir(proj["id"]) / "exports"


def test_pin_off_the_image_is_refused(ws, proj):
    with pytest.raises(service.RetouchError):
        ws.add_pin(proj["id"], shape="rect", geom={"x": 900, "y": 900, "w": 10, "h": 10})


@pytest.mark.parametrize("bad", ["../etc", "rt_123", "rt_zzzzzzzzzzzz", ""])
def test_project_ids_cannot_escape_the_workspace(ws, bad):
    with pytest.raises(service.RetouchError):
        ws.project_dir(bad)


def test_views_number_every_mark(ws, proj):
    mark(ws, proj)
    mark(ws, proj)
    project, overview, crops = ws.views(proj["id"])
    assert [n for n, _ in crops] == [1, 2] and max(overview.size) <= 1568


def flat_project(ws, color=(100, 100, 100), size=(400, 300)):
    return ws.create_project("flat.png", png_bytes(Image.new("RGB", size, color)))


def test_tonal_adjust_fades_toward_the_mark_edge_with_no_outline(ws):
    proj = flat_project(ws)
    n = ws.add_pin(proj["id"], shape="rect", geom={"x": 100, "y": 75, "w": 200, "h": 150}, note="brighter")["number"]
    ws.edit(proj["id"], n, method="adjust", ops=[{"op": "exposure", "value": 1}])
    after, _ = ws.load(proj["id"])
    centre = after.getpixel((200, 150))[0] - 100
    just_inside = after.getpixel((102, 150))[0] - 100
    assert centre > 80  # the middle got the full change
    assert just_inside < 0.15 * centre  # no hard step at the border


def test_redaction_covers_every_pixel_of_the_mark(ws):
    proj = flat_project(ws, color=(200, 180, 160))
    n = ws.add_pin(proj["id"], shape="ellipse", geom={"x": 120, "y": 80, "w": 160, "h": 120}, note="hide this")["number"]
    ws.edit(proj["id"], n, method="adjust", ops=[{"op": "redact", "value": "black"}])
    after, _ = ws.load(proj["id"])
    pin = ws.project(proj["id"])["pins"][0]
    mask = ws.mask(pin, after.size)
    inside = [px for px, m in zip(after.convert("RGB").tobytes()[0::3], mask.tobytes()) if m]
    assert inside and max(inside) == 0  # every marked pixel fully black, rim included


def test_an_open_mark_can_be_moved_but_a_sent_one_cannot(ws, proj):
    n = mark(ws, proj)["number"]
    moved = ws.update_pin(proj["id"], n, geom={"x": 150, "y": 100, "w": 80, "h": 60})
    assert moved["geom"]["x"] == pytest.approx(150 / 320) and moved["geom"]["y"] == pytest.approx(100 / 240)

    ws.send(proj["id"], [n])
    with pytest.raises(service.RetouchError, match="already sent"):
        ws.update_pin(proj["id"], n, geom={"x": 10, "y": 10, "w": 80, "h": 60})
    assert ws.project(proj["id"])["pins"][0]["geom"]["x"] == pytest.approx(150 / 320)


def test_moving_a_mark_off_the_image_is_refused(ws, proj):
    n = mark(ws, proj)["number"]
    with pytest.raises(service.RetouchError):
        ws.update_pin(proj["id"], n, geom={"x": 900, "y": 900, "w": 80, "h": 60})


def test_a_queued_mark_can_be_put_back_when_there_was_no_chat_to_send_it_to(ws, proj):
    n = mark(ws, proj)["number"]
    ws.send(proj["id"], [n])
    assert ws.user_action(proj["id"], n, "unsend")["status"] == "open"
    with pytest.raises(store.StoreError):
        ws.user_action(proj["id"], n, "unsend")  # only a queued mark can be taken back
