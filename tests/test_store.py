"""L2/L5: only the user closes a mark; versions are never lost; bad edits are never saved."""

import sqlite3

import pytest

from retouch import store


@pytest.fixture
def db():
    return store.init(sqlite3.connect(":memory:"))


@pytest.fixture
def project(db):
    return store.create_project(db, title="trip", src_name="trip.jpg", size=(320, 240), first_version_file="v0.png")


def pin(db, project, **kw):
    return store.add_pin(db, project["id"], shape="ellipse", geom={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                         note="remove the bag", **kw)


@pytest.mark.parametrize("status", ["resolved", "wont_fix", "open", "queued", "bogus"])
def test_agent_cannot_close_or_reset_a_mark(db, project, status):
    n = pin(db, project)["number"]
    with pytest.raises(store.StoreError):
        store.agent_set_status(db, project["id"], n, status)


def test_agent_cannot_touch_a_mark_the_user_closed(db, project):
    n = pin(db, project)["number"]
    store.agent_set_status(db, project["id"], n, "needs_review")
    store.user_action(db, project["id"], n, "accept")
    with pytest.raises(store.StoreError):
        store.agent_set_status(db, project["id"], n, "working")


def test_needs_more_work_reopens_with_round_plus_one_and_records_reply(db, project):
    n = pin(db, project)["number"]
    store.user_action(db, project["id"], n, "send")
    store.agent_set_status(db, project["id"], n, "needs_review")
    after = store.user_action(db, project["id"], n, "more_work", "there's still a shadow")
    assert after["status"] == "open" and after["round"] == 2
    assert after["thread"][-1] == {**after["thread"][-1], "author": "user", "body": "there's still a shadow"}


def test_user_action_checks_the_current_status(db, project):
    n = pin(db, project)["number"]
    with pytest.raises(store.StoreError):
        store.user_action(db, project["id"], n, "more_work")  # nothing to review yet


def test_version_refused_when_pixels_outside_changed(db, project):
    with pytest.raises(store.StoreError):
        store.add_version(db, project["id"], file="v1.png", author="agent", pin=1, method="adjust",
                          summary="x", outside_changed_px=3, inside_changed_px=100)
    assert len(store.get_project(db, project["id"])["versions"]) == 1


def test_revert_restores_parent_and_keeps_history(db, project):
    n = pin(db, project)["number"]
    original = project["current_version"]
    v1 = store.add_version(db, project["id"], file="v1.png", author="agent", pin=n, method="adjust",
                           summary="brightened", outside_changed_px=0, inside_changed_px=50)
    reverted = store.revert(db, project["id"], pin=n)
    assert reverted["current_version"] == original
    assert [v["id"] for v in reverted["versions"]] == [original, v1]
    assert reverted["pins"][0]["status"] == "open"
    assert store.revert(db, project["id"], to_version=v1)["current_version"] == v1  # undo the revert


def test_pins_number_sequentially_and_thread_orders(db, project):
    a, b = pin(db, project), pin(db, project, intent="ask")
    assert (a["number"], b["number"]) == (1, 2)
    store.add_message(db, project["id"], 2, "agent", "first")
    store.add_message(db, project["id"], 2, "user", "second")
    assert [m["body"] for m in store.get_project(db, project["id"])["pins"][1]["thread"]] == ["first", "second"]


def test_edit_made_on_a_stale_version_is_refused(db, project):
    n = pin(db, project)["number"]
    base = project["current_version"]
    store.add_version(db, project["id"], file="v1.png", author="agent", pin=n, method="adjust", summary="a",
                      outside_changed_px=0, inside_changed_px=1, expected_parent=base)
    with pytest.raises(store.VersionConflict):
        store.add_version(db, project["id"], file="v2.png", author="agent", pin=n, method="adjust", summary="b",
                          outside_changed_px=0, inside_changed_px=1, expected_parent=base)
    assert len(store.get_project(db, project["id"])["versions"]) == 2
