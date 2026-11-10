"""A pasted image (a screenshot off the clipboard) opens as a scratch project: kept only when the user says so (Keep, Save, or by
working on it), otherwise it goes."""
import io
import time

import pytest

from conftest import noise
from retouch import service


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


def png(size=(64, 48), seed=3):
    buf = io.BytesIO()
    noise(size, seed).save(buf, "PNG")
    return buf.getvalue()


def test_a_pasted_image_is_a_scratch_project_until_kept(ws):
    proj = ws.create_project("Screenshot-1.png", png(), scratch=True)
    assert proj["scratch"] is True
    assert ws.projects()[0]["scratch"] is True
    assert ws.keep(proj["id"])["scratch"] is False


def test_pasting_sweeps_old_untouched_pastes(ws):
    old = ws.create_project("old.png", png(), scratch=True)
    with ws.db() as conn:
        conn.execute("UPDATE projects SET updated_at = ?", (time.time() - ws.SCRATCH_TTL - 60,))
        conn.commit()
    new = ws.create_project("new.png", png(seed=4), scratch=True)
    assert [p["id"] for p in ws.projects()] == [new["id"]]
    assert not ws.project_dir(old["id"]).exists()


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
