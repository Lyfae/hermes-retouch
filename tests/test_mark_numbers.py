"""Mark numbers are identities: versions and the chat thread refer to a mark by its number, so a
deleted mark's number is retired, never handed to the next mark."""
import io

import pytest

from conftest import noise
from retouch import service


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


def _png():
    buf = io.BytesIO()
    noise((64, 48), 5).save(buf, "PNG")
    return buf.getvalue()


def test_a_deleted_marks_number_is_never_reused(ws):
    pid = ws.create_project("p.png", _png())["id"]
    rect = {"x": 2, "y": 2, "w": 10, "h": 10}
    assert ws.add_pin(pid, shape="rect", geom=rect)["number"] == 1
    assert ws.add_pin(pid, shape="rect", geom=rect)["number"] == 2
    ws.delete_pin(pid, 2)
    assert ws.add_pin(pid, shape="rect", geom=rect)["number"] == 3
    ws.delete_pin(pid, 1)
    ws.delete_pin(pid, 3)
    assert ws.add_pin(pid, shape="rect", geom=rect)["number"] == 4


def test_an_edited_then_deleted_mark_keeps_its_version_attribution(ws):
    pid = ws.create_project("p.png", _png())["id"]
    ws.add_pin(pid, shape="rect", geom={"x": 0, "y": 0, "w": 8, "h": 8})
    ws.edit(pid, 1, method="adjust", ops=[{"op": "exposure", "value": 1.0}])
    ws.delete_pin(pid, 1)
    new = ws.add_pin(pid, shape="rect", geom={"x": 30, "y": 30, "w": 8, "h": 8})
    versions = ws.project(pid)["versions"]
    assert versions[-1]["pin"] == 1 and new["number"] != 1
