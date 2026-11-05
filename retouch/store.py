"""Projects, marks, threads and versions in SQLite, plus the status rules for marks.

Two actors touch a mark. The user (every REST call from the pane) and the agent (every tool call).
The agent can only say what it did or ask; only the user can close a mark.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, src_name TEXT NOT NULL, width INT NOT NULL, height INT NOT NULL,
  current_version TEXT, session_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, seq INT NOT NULL, parent TEXT, file TEXT NOT NULL,
  author TEXT NOT NULL CHECK (author IN ('user', 'agent')), pin INT, method TEXT, summary TEXT,
  outside_changed_px INT, inside_changed_px INT, created_at REAL NOT NULL, spill TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS pins (
  project_id TEXT NOT NULL, number INT NOT NULL,
  shape TEXT NOT NULL CHECK (shape IN ('ellipse', 'rect', 'lasso', 'brush')), geom TEXT NOT NULL,
  intent TEXT NOT NULL CHECK (intent IN ('fix', 'remove', 'enhance', 'ask')), note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, round INT NOT NULL DEFAULT 1, result_version TEXT, created_at REAL NOT NULL,
  PRIMARY KEY (project_id, number));
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, pin INT NOT NULL,
  author TEXT NOT NULL CHECK (author IN ('user', 'agent')), body TEXT NOT NULL, version TEXT,
  created_at REAL NOT NULL, issues TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS messages_by_pin ON messages (project_id, pin, created_at);
CREATE INDEX IF NOT EXISTS versions_by_project ON versions (project_id, seq);
"""
# Columns added after a table first shipped: (table, column, definition). Applied once per database.
ADDED_COLUMNS = (("messages", "issues", "TEXT NOT NULL DEFAULT ''"), ("versions", "spill", "TEXT NOT NULL DEFAULT ''"))

STATUSES = ("open", "queued", "working", "needs_review", "question", "resolved", "wont_fix")
AGENT_SETS = {"working", "needs_review", "question"}
CLOSED = {"resolved", "wont_fix"}
# user action -> (allowed from, resulting status)
USER_ACTIONS = {
    "send": ({"open"}, "queued"),
    "unsend": ({"queued"}, "open"),  # the pane queued a mark but had no chat to send it to
    "accept": ({"needs_review", "working", "question", "queued"}, "resolved"),
    "more_work": ({"needs_review", "question", "resolved"}, "open"),
    "wont_fix": (set(STATUSES) - {"wont_fix"}, "wont_fix"),
    "reopen": ({"resolved", "wont_fix"}, "open"),
}


class StoreError(ValueError):
    pass


class VersionConflict(StoreError):
    """Another edit became current while this one was being made; redo it on the new current version."""


def init(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            with conn:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return conn


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _touch(conn, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))


def create_project(conn, *, title: str, src_name: str, size: tuple[int, int], first_version_file: str,
                   project_id: str | None = None) -> dict:
    pid = project_id or new_id("rt")
    vid = new_id("v")
    now = _now()
    with conn:
        conn.execute("INSERT INTO projects (id, title, src_name, width, height, current_version, session_id, created_at, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                     (pid, title, src_name, size[0], size[1], vid, now, now))
        conn.execute("INSERT INTO versions (id, project_id, seq, parent, file, author, pin, method, summary, "
                     "outside_changed_px, inside_changed_px, created_at) "
                     "VALUES (?, ?, 0, NULL, ?, 'user', NULL, 'original', 'Original', 0, 0, ?)",
                     (vid, pid, first_version_file, now))
    return get_project(conn, pid)


def require_project(conn, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (str(project_id),)).fetchone()
    if row is None:
        raise StoreError(f"no Retouch project {project_id!r}")
    return row


def require_pin(conn, project_id: str, number: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM pins WHERE project_id = ? AND number = ?", (project_id, int(number))).fetchone()
    if row is None:
        raise StoreError(f"project {project_id} has no mark #{number}")
    return row


def version(conn, project_id: str, version_id: str | None = None) -> sqlite3.Row:
    vid = version_id or require_project(conn, project_id)["current_version"]
    row = conn.execute("SELECT * FROM versions WHERE id = ? AND project_id = ?", (vid, project_id)).fetchone()
    if row is None:
        raise StoreError(f"project {project_id} has no version {vid!r}")
    return row


def add_pin(conn, project_id: str, *, shape: str, geom: dict, intent: str = "fix", note: str = "") -> dict:
    require_project(conn, project_id)
    if intent not in ("fix", "remove", "enhance", "ask"):
        raise StoreError("intent must be fix, remove, enhance or ask")
    with conn:
        n = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM pins WHERE project_id = ?", (project_id,)).fetchone()[0]
        conn.execute("INSERT INTO pins (project_id, number, shape, geom, intent, note, status, round, result_version, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, NULL, ?)",
                     (project_id, n, shape, json.dumps(geom), intent, note.strip(), _now()))
        _touch(conn, project_id)
    return pin_dict(conn, require_pin(conn, project_id, n))


def update_pin(conn, project_id: str, number: int, **fields: Any) -> dict:
    pin = require_pin(conn, project_id, number)
    if pin["status"] in CLOSED:
        raise StoreError(f"mark #{number} is closed; reopen it first")
    allowed = {"note", "intent", "shape", "geom"}
    sets = {k: (json.dumps(v) if k == "geom" else v) for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return pin_dict(conn, pin)
    with conn:
        conn.execute(f"UPDATE pins SET {', '.join(f'{k} = ?' for k in sets)} WHERE project_id = ? AND number = ?",
                     (*sets.values(), project_id, int(number)))
        _touch(conn, project_id)
    return pin_dict(conn, require_pin(conn, project_id, number))


def delete_pin(conn, project_id: str, number: int) -> None:
    require_pin(conn, project_id, number)
    with conn:
        conn.execute("DELETE FROM pins WHERE project_id = ? AND number = ?", (project_id, int(number)))
        conn.execute("DELETE FROM messages WHERE project_id = ? AND pin = ?", (project_id, int(number)))
        _touch(conn, project_id)


def agent_set_status(conn, project_id: str, number: int, status: str) -> dict:
    """The agent's only door to a mark's status: working, needs_review or question."""
    if status not in AGENT_SETS:
        raise StoreError(f"the agent can set status to {', '.join(sorted(AGENT_SETS))} only; the user closes marks")
    pin = require_pin(conn, project_id, number)
    if pin["status"] in CLOSED:
        raise StoreError(f"mark #{number} is {pin['status']} (closed by the user); ask them to reopen it")
    with conn:
        conn.execute("UPDATE pins SET status = ? WHERE project_id = ? AND number = ?", (status, project_id, int(number)))
        _touch(conn, project_id)
    return pin_dict(conn, require_pin(conn, project_id, number))


def restore_status(conn, project_id: str, number: int, status: str) -> None:
    """Put a mark back to the status it had before a failed edit. Internal to the edit pipeline;
    never reachable from a tool argument, so it does not widen what the agent can set."""
    if status not in STATUSES:
        raise StoreError(f"unknown status {status!r}")
    with conn:
        conn.execute("UPDATE pins SET status = ? WHERE project_id = ? AND number = ? AND status = 'working'",
                     (status, project_id, int(number)))


def user_action(conn, project_id: str, number: int, action: str, message: str = "") -> dict:
    if action not in USER_ACTIONS:
        raise StoreError(f"action must be one of {', '.join(USER_ACTIONS)}")
    allowed_from, to = USER_ACTIONS[action]
    pin = require_pin(conn, project_id, number)
    if pin["status"] not in allowed_from:
        raise StoreError(f"cannot {action.replace('_', ' ')} mark #{number} while it is {pin['status']}")
    bump = 1 if action == "more_work" else 0
    with conn:
        conn.execute("UPDATE pins SET status = ?, round = round + ? WHERE project_id = ? AND number = ?",
                     (to, bump, project_id, int(number)))
        if message.strip():
            _add_message(conn, project_id, number, "user", message.strip(), None)
        _touch(conn, project_id)
    return pin_dict(conn, require_pin(conn, project_id, number))


def _add_message(conn, project_id, number, author, body, version_id, issues=""):
    conn.execute("INSERT INTO messages (id, project_id, pin, author, body, version, created_at, issues) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (new_id("m"), project_id, int(number), author, body, version_id, _now(), issues))


def add_message(conn, project_id: str, number: int, author: str, body: str, version_id: str | None = None,
                issues: str = "") -> None:
    """``issues``: what is still visibly wrong after the agent's edit ('' when nothing is)."""
    if author not in ("user", "agent"):
        raise StoreError("author must be user or agent")
    if not body.strip():
        raise StoreError("message is empty")
    require_pin(conn, project_id, number)
    with conn:
        _add_message(conn, project_id, number, author, body.strip(), version_id, issues.strip())
        _touch(conn, project_id)


def result_spill(conn, project_id: str, number: int) -> list[str]:
    """The edge-check sides of the mark's latest edit ([] when it has none or none was flagged)."""
    row = conn.execute("SELECT v.spill FROM pins p JOIN versions v ON v.id = p.result_version "
                       "WHERE p.project_id = ? AND p.number = ?", (project_id, int(number))).fetchone()
    return [s for s in (row["spill"] if row else "").split(",") if s]


def add_version(conn, project_id: str, *, file: str, author: str, pin: int | None, method: str, summary: str,
                outside_changed_px: int, inside_changed_px: int, expected_parent: str | None = None,
                version_id: str | None = None, spill: list[str] | tuple[str, ...] = ()) -> str:
    """Record a new current version. Refuses any edit that changed pixels outside its mark, and (with
    ``expected_parent``) any edit made on a version that is no longer current. ``spill``: sides of the
    mark the edit wanted to change past (see ``composite.spill_sides``)."""
    if outside_changed_px != 0:
        raise StoreError(f"refusing to save: {outside_changed_px} pixels outside the mark changed")
    vid = version_id or new_id("v")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        proj = require_project(conn, project_id)
        if expected_parent is not None and proj["current_version"] != expected_parent:
            raise VersionConflict(f"project {project_id} changed while editing")
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM versions WHERE project_id = ?", (project_id,)).fetchone()[0]
        conn.execute("INSERT INTO versions (id, project_id, seq, parent, file, author, pin, method, summary, "
                     "outside_changed_px, inside_changed_px, created_at, spill) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (vid, project_id, seq, proj["current_version"], file, author, pin, method, summary,
                      outside_changed_px, inside_changed_px, _now(), ",".join(spill)))
        conn.execute("UPDATE projects SET current_version = ? WHERE id = ?", (vid, project_id))
        if pin is not None:
            conn.execute("UPDATE pins SET result_version = ? WHERE project_id = ? AND number = ?", (vid, project_id, pin))
        _touch(conn, project_id)
    return vid


def revert(conn, project_id: str, *, to_version: str | None = None, pin: int | None = None) -> dict:
    """Make an earlier version current again (the parent of the current one by default). Versions are
    never deleted, so a revert can itself be undone by reverting to the later version id."""
    proj = require_project(conn, project_id)
    if to_version is None:
        current = version(conn, project_id)
        if current["parent"] is None:
            raise StoreError("already at the original")
        to_version = current["parent"]
    version(conn, project_id, to_version)
    with conn:
        conn.execute("UPDATE projects SET current_version = ? WHERE id = ?", (to_version, proj["id"]))
        if pin is not None:
            conn.execute("UPDATE pins SET status = 'open', result_version = NULL WHERE project_id = ? AND number = ?",
                         (project_id, int(pin)))
        _touch(conn, project_id)
    return get_project(conn, project_id)


def set_session(conn, project_id: str, session_id: str | None) -> None:
    with conn:
        conn.execute("UPDATE projects SET session_id = ? WHERE id = ?", (session_id, project_id))


def pin_dict(conn, row: sqlite3.Row) -> dict:
    msgs = conn.execute("SELECT author, body, version, issues, created_at FROM messages WHERE project_id = ? AND pin = ? "
                        "ORDER BY created_at", (row["project_id"], row["number"])).fetchall()
    return {"number": row["number"], "shape": row["shape"], "geom": json.loads(row["geom"]), "intent": row["intent"],
            "note": row["note"], "status": row["status"], "round": row["round"], "result_version": row["result_version"],
            "thread": [dict(m) for m in msgs]}


def get_project(conn, project_id: str) -> dict:
    p = require_project(conn, project_id)
    pins = conn.execute("SELECT * FROM pins WHERE project_id = ? ORDER BY number", (project_id,)).fetchall()
    versions = conn.execute("SELECT id, seq, parent, author, pin, method, summary, outside_changed_px, "
                            "inside_changed_px, spill, created_at FROM versions WHERE project_id = ? ORDER BY seq",
                            (project_id,)).fetchall()
    return {"id": p["id"], "title": p["title"], "src_name": p["src_name"], "width": p["width"], "height": p["height"],
            "current_version": p["current_version"], "session_id": p["session_id"], "updated_at": p["updated_at"],
            "pins": [pin_dict(conn, r) for r in pins], "versions": [dict(v) for v in versions]}


def list_projects(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT p.id, p.title, p.width, p.height, p.updated_at, "
                        "(SELECT COUNT(*) FROM pins WHERE project_id = p.id AND status NOT IN ('resolved', 'wont_fix')) "
                        "AS open_marks FROM projects p ORDER BY p.updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
