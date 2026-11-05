"""Honest review: the agent must say what is still wrong, generated fills leave no ghost at the rim,
and databases from before ``remaining_issues`` keep working."""

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from retouch import service, store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path)


@pytest.fixture
def plugin_tools(ws, monkeypatch):
    """The plugin's tools module, imported the way Hermes imports the plugin (as a package)."""
    spec = importlib.util.spec_from_file_location("retouch_plugin_under_test", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, pkg)
    spec.loader.exec_module(pkg)
    tools = importlib.import_module(f"{spec.name}.tools")
    monkeypatch.setattr(tools.service.Workspace, "current", staticmethod(lambda: ws))
    return tools


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_remaining_issues_reach_the_users_thread(ws):
    proj = ws.create_project("a.png", png_bytes(Image.new("RGB", (200, 150), "gray")))
    n = ws.add_pin(proj["id"], shape="rect", geom={"x": 40, "y": 40, "w": 60, "h": 40}, note="remove it")["number"]
    pin = ws.reply(proj["id"], n, "Removed the bag.", "needs_review", issues="a faint outline remains at the left edge")
    assert pin["thread"][-1]["issues"] == "a faint outline remains at the left edge"


def test_reply_tool_refuses_to_omit_remaining_issues(ws, plugin_tools):
    proj = ws.create_project("a.png", png_bytes(Image.new("RGB", (200, 150), "gray")))
    n = ws.add_pin(proj["id"], shape="rect", geom={"x": 40, "y": 40, "w": 60, "h": 40}, note="remove it")["number"]
    args = {"project_id": proj["id"], "pin": n, "message": "Done.", "status": "needs_review"}

    refused = json.loads(plugin_tools.handle_reply(args))
    assert "remaining_issues" in refused["error"]
    assert ws.project(proj["id"])["pins"][0]["thread"] == []

    ok = json.loads(plugin_tools.handle_reply(args | {"remaining_issues": ""}))
    assert ok["pin"]["status"] == "needs_review"


def test_generated_fill_leaves_no_ghost_of_the_removed_object_near_the_rim(ws):
    # A white object reaching to 5 px inside the mark on flat grey; the "model" returns clean grey.
    size, box = (1280, 720), (900, 400, 1100, 560)
    img = Image.new("RGB", size, (100, 100, 100))
    ImageDraw.Draw(img).ellipse((box[0] + 5, box[1] + 5, box[2] - 5, box[3] - 5), fill="white")
    proj = ws.create_project("bag.png", png_bytes(img))
    n = ws.add_pin(proj["id"], shape="ellipse", intent="remove", note="remove the bag",
                   geom={"x": box[0], "y": box[1], "w": box[2] - box[0], "h": box[3] - box[1]})["number"]

    ws.edit(proj["id"], n, method="generate",
            generator=lambda path, prompt: Image.new("RGB", (1024, 1024), (100, 100, 100)))

    after, _ = ws.load(proj["id"])
    brightest = max(after.convert("L").crop(box).tobytes())
    assert brightest < 112  # was 255; anything near white is the object showing through the fade


def test_a_models_exposure_drift_is_corrected_from_the_untouched_surroundings(ws):
    size, box = (640, 480), (250, 180, 390, 300)
    img = Image.new("RGB", size, (100, 100, 100))
    ImageDraw.Draw(img).ellipse(box, fill="white")
    proj = ws.create_project("drift.png", png_bytes(img))
    n = ws.add_pin(proj["id"], shape="ellipse", intent="remove", note="remove the ball",
                   geom={"x": box[0] - 4, "y": box[1] - 4, "w": box[2] - box[0] + 8, "h": box[3] - box[1] + 8})["number"]

    # The model removed the ball but returned the whole crop 30 levels too bright.
    ws.edit(proj["id"], n, method="generate",
            generator=lambda path, prompt: Image.new("RGB", (1024, 1024), (130, 130, 130)))

    after, _ = ws.load(proj["id"])
    assert abs(after.getpixel(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2))[0] - 100) <= 2


def test_a_database_from_before_remaining_issues_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA.replace(", issues TEXT NOT NULL DEFAULT ''", ""))
    old.execute("INSERT INTO messages (id, project_id, pin, author, body, version, created_at) "
                "VALUES ('m_1', 'rt_1', 1, 'user', 'make it brighter', NULL, 0)")
    old.commit()
    old.close()

    conn = store.init(sqlite3.connect(path))
    row = conn.execute("SELECT body, issues FROM messages").fetchone()
    assert (row["body"], row["issues"]) == ("make it brighter", "")
    store.init(conn)  # idempotent


def test_edit_tool_saves_a_review_image_the_agent_can_zoom_into(ws, plugin_tools):
    proj = ws.create_project("a.png", png_bytes(Image.new("RGB", (320, 240), (90, 90, 90))))
    n = ws.add_pin(proj["id"], shape="ellipse", geom={"x": 100, "y": 60, "w": 120, "h": 100}, note="brighter")["number"]
    out = plugin_tools.make_edit_handler(ctx=None)({"project_id": proj["id"], "pin": n, "method": "adjust",
                                                     "ops": [{"op": "exposure", "value": 1}]})
    summary = json.loads(out["text_summary"])
    review = Path(summary["review_image"])
    assert review.is_absolute() and review.is_file()
    assert Image.open(review).size[0] > Image.open(review).size[1]  # before and after, side by side
