"""A screenshot opens as a scratch project: kept only when the user says so (Keep, Save, or by
working on it), otherwise it goes."""
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import noise
from retouch import service


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


def png(size=(64, 48), seed=3):
    buf = io.BytesIO()
    noise(size, seed).save(buf, "PNG")
    return buf.getvalue()


def fake_picker(monkeypatch, run):
    """Stand in for macOS `screencapture`; `run(cmd)` decides what it leaves behind and returns."""
    fake = type("S", (), {"run": staticmethod(lambda cmd, **k: run(cmd)),
                          "TimeoutExpired": service.subprocess.TimeoutExpired})
    monkeypatch.setattr(service, "subprocess", fake)
    monkeypatch.setattr(service.Workspace, "can_screenshot", staticmethod(lambda: True))


def captured(cmd):
    Image.open(io.BytesIO(png())).save(cmd[-1])
    return SimpleNamespace(returncode=0, stderr="")


def cancelled(_cmd):
    return SimpleNamespace(returncode=0, stderr="")


def test_a_screenshot_is_a_scratch_project_until_kept(ws, monkeypatch):
    fake_picker(monkeypatch, captured)
    proj = ws.screenshot()
    assert proj["scratch"] is True
    assert proj["title"].startswith("Screenshot-")
    assert ws.projects()[0]["scratch"] is True
    kept = ws.keep(proj["id"])
    assert kept["scratch"] is False


def test_cancelling_the_picker_opens_nothing(ws, monkeypatch):
    fake_picker(monkeypatch, cancelled)
    assert ws.screenshot() is None
    assert ws.projects() == []


def test_a_failed_capture_is_an_error_not_a_cancel(ws, monkeypatch):
    fake_picker(monkeypatch, lambda _cmd: SimpleNamespace(returncode=1, stderr="could not create image from display"))
    with pytest.raises(service.RetouchError, match="could not create image"):
        ws.screenshot()
    assert ws.projects() == []


def test_a_picker_left_open_too_long_is_an_error(ws, monkeypatch):
    def hang(cmd):
        raise service.subprocess.TimeoutExpired(cmd, 55)

    fake_picker(monkeypatch, hang)
    with pytest.raises(service.RetouchError, match="within"):
        ws.screenshot()


def test_discard_removes_the_project_and_its_files(ws):
    proj = ws.create_project("shot.png", png(), scratch=True)
    pdir = ws.project_dir(proj["id"])
    assert pdir.is_dir()
    ws.discard(proj["id"])
    assert not pdir.exists()
    assert ws.projects() == []
    with pytest.raises((service.RetouchError, service.store.StoreError)):
        ws.project(proj["id"])


def test_discard_refuses_a_kept_project(ws):
    proj = ws.create_project("shot.png", png(), scratch=True)
    ws.keep(proj["id"])
    with pytest.raises(service.RetouchError, match="kept"):
        ws.discard(proj["id"])
    assert ws.project(proj["id"])["id"] == proj["id"]
    assert ws.project_dir(proj["id"]).is_dir()


def test_saving_a_scratch_project_keeps_it(ws):
    proj = ws.create_project("shot.png", png(), scratch=True)
    ws.export_and_keep(proj["id"], fmt="png")
    assert ws.project(proj["id"])["scratch"] is False


def test_marking_or_editing_a_scratch_project_keeps_it(ws):
    a = ws.create_project("a.png", png(seed=7), scratch=True)
    ws.add_pin(a["id"], shape="rect", geom={"x": 2, "y": 2, "w": 10, "h": 10})
    assert ws.project(a["id"])["scratch"] is False
    b = ws.create_project("b.png", png(seed=8), scratch=True)
    ws.add_pin(b["id"], shape="rect", geom={"x": 2, "y": 2, "w": 10, "h": 10})
    ws.edit(b["id"], 1, method="adjust", ops=[{"op": "exposure", "value": 1.0}])
    assert ws.project(b["id"])["scratch"] is False


def test_only_untouched_old_scratch_projects_are_swept(ws):
    old = ws.create_project("old.png", png(seed=1), scratch=True)
    kept = ws.create_project("kept.png", png(seed=2), scratch=True)
    ws.keep(kept["id"])
    fresh = ws.create_project("fresh.png", png(seed=4), scratch=True)
    with ws.db() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id IN (?, ?)",
                     (time.time() - ws.SCRATCH_TTL - 60, old["id"], kept["id"]))
        conn.commit()
    swept = ws.sweep_scratch()
    assert swept == [old["id"]]
    assert {p["id"] for p in ws.projects()} == {kept["id"], fresh["id"]}
    assert not ws.project_dir(old["id"]).exists()


def test_without_a_picker_the_user_is_told_to_paste(ws, monkeypatch):
    monkeypatch.setattr(service.Workspace, "can_screenshot", staticmethod(lambda: False))
    with pytest.raises(service.RetouchError, match="paste"):
        ws.screenshot()
