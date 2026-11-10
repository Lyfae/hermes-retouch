"""Redo walks forward again after Undo, and never invents a step that was not undone."""
import io

import pytest

from conftest import noise
from retouch import service


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


@pytest.fixture
def edited(ws):
    """A project with two edits; returns (project id, [original, edit 1, edit 2])."""
    buf = io.BytesIO()
    noise((64, 48), 5).save(buf, "PNG")
    pid = ws.create_project("p.png", buf.getvalue())["id"]
    ws.add_pin(pid, shape="rect", geom={"x": 2, "y": 2, "w": 20, "h": 20}, units="px")
    ws.add_pin(pid, shape="rect", geom={"x": 30, "y": 20, "w": 20, "h": 20}, units="px")
    ws.edit(pid, 1, method="adjust", ops=[{"op": "exposure", "value": 1.0}])
    ws.edit(pid, 2, method="adjust", ops=[{"op": "grayscale", "value": 1.0}])
    return pid, [v["id"] for v in ws.project(pid)["versions"]]


def test_redo_returns_to_the_version_undo_left(ws, edited):
    pid, (original, first, second) = edited
    ws.revert(pid)
    assert ws.project(pid)["current_version"] == first
    assert ws.redo(pid)["current_version"] == second


def test_undo_twice_then_redo_twice_walks_the_same_path(ws, edited):
    pid, (original, first, second) = edited
    ws.revert(pid)
    ws.revert(pid)
    assert ws.project(pid)["current_version"] == original
    assert ws.redo(pid)["current_version"] == first
    assert ws.redo(pid)["current_version"] == second


def test_nothing_to_redo_at_the_newest_version(ws, edited):
    pid, _ = edited
    with pytest.raises((service.RetouchError, service.store.StoreError), match="nothing to redo"):
        ws.redo(pid)


def test_redo_follows_the_newest_edit_after_a_branch(ws, edited):
    """Undo, then a new edit: the undone edit is off the path. Undo again and Redo returns to the
    NEW edit, not the abandoned one."""
    pid, (original, first, second) = edited
    ws.revert(pid)
    ws.add_pin(pid, shape="rect", geom={"x": 40, "y": 2, "w": 10, "h": 10}, units="px")
    newer = ws.edit(pid, 3, method="adjust", ops=[{"op": "exposure", "value": -1.0}]).version_id
    ws.revert(pid)
    assert ws.redo(pid)["current_version"] == newer
    assert {v["id"] for v in ws.project(pid)["versions"]} >= {second, newer}


def test_redo_leaves_marks_as_the_user_left_them(ws, edited):
    pid, _ = edited
    before = {p["number"]: p["status"] for p in ws.project(pid)["pins"]}
    ws.revert(pid)
    ws.redo(pid)
    assert {p["number"]: p["status"] for p in ws.project(pid)["pins"]} == before
