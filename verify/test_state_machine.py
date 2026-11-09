"""Stateful check: the mark/version state machine keeps its promises under EVERY order of user and
agent calls Hypothesis can generate (up to 40 steps per run, 60 runs), on the real store and service.

Promises checked after every step:
  P1  Only the user closes a mark. 'resolved' / 'wont_fix' are entered by user actions alone; the
      agent's door (agent_set_status, the edit pipeline, reply) never produces them and cannot move
      a closed mark.
  P2  A mark's outline is frozen once it leaves 'open': its geometry does not change until the user
      brings it back to open (more_work, reopen, revert, unsend).
  P3  Versions form a tree rooted at the original: no version is ever deleted, each child is newer
      than its parent, and the current version reaches the original by parent links.
  P4  Every stored version changed 0 pixels outside the outline its mark had when the edit was
      made, checked against the version files themselves (the pixel guarantee end to end, not the
      stored count).
  P5  A mark's round increases only through 'more_work'.
  P7  A mark number is an identity: once handed out it is never assigned again, even after a delete
      (versions and the chat thread refer to marks by number).

Not checked here: `restore_status` after a failed edit (its guard matters only with concurrent
writers, and these runs are sequential); the generate and from_file edit methods (adjust only).

Run:  uv run --no-project --python 3.14 --with "pillow==13.0.0" --with pytest --with hypothesis \
        pytest verify -q
"""
from __future__ import annotations

import io
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, precondition, rule  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from retouch import composite, service, store  # noqa: E402

W, H = 64, 48
USER_ACTIONS = sorted(store.USER_ACTIONS)
AGENT_STATUSES = sorted(store.AGENT_SETS)
SIZE_PX = st.integers(4, 40)
POS_PX = st.integers(-8, 60)


def noise_png(seed):
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", (W, H), bytes(rnd.getrandbits(8) for _ in range(W * H * 3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class RetouchMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="retouch-verify-")
        self.ws = service.Workspace(Path(self.tmp.name))
        self.pid = self.ws.create_project("probe.png", noise_png(1))["id"]
        # model: what the user did, as the oracle for P1/P2/P5
        self.closed_by_user: set[int] = set()
        self.frozen_geom: dict[int, str] = {}  # number -> json geom when it left 'open'
        self.rounds: dict[int, int] = {}
        self.edit_outline: dict[str, tuple[str, dict]] = {}  # version id -> (shape, geom) of the mark when edited
        self.numbers_ever: set[int] = set()
        self.versions_seen: set[str] = set()

    marks = Bundle("marks")

    def teardown(self):
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------------------------------
    def project(self):
        return self.ws.project(self.pid)

    def pin(self, n):
        return next((p for p in self.project()["pins"] if p["number"] == n), None)

    def _note_transition(self, n, before, after, *, by_user):
        if after in store.CLOSED:
            if by_user:
                self.closed_by_user.add(n)
            # an agent transition into a closed status is left unrecorded, so P1 fails on it
        elif by_user:
            self.closed_by_user.discard(n)
            # ...and an agent transition OUT of a closed status leaves the flag set, so P1 fails too
        if before == "open" and after != "open":
            self.frozen_geom[n] = repr(self.pin(n)["geom"])
        if after == "open":
            self.frozen_geom.pop(n, None)

    # -- user rules -------------------------------------------------------------------------------
    @rule(target=marks, x=POS_PX, y=POS_PX, w=SIZE_PX, h=SIZE_PX, shape=st.sampled_from(["rect", "ellipse"]),
          intent=st.sampled_from(["fix", "remove", "enhance", "ask"]))
    def add_mark(self, x, y, w, h, shape, intent):
        try:
            pin = self.ws.add_pin(self.pid, shape=shape, geom={"x": x, "y": y, "w": w, "h": h}, units="px",
                                  intent=intent, note="probe")
        except service.RetouchError:
            return  # a mark covering no pixel is refused; nothing to track
        assert pin["number"] not in self.numbers_ever, f"mark number {pin['number']} was handed out twice (P7)"
        self.numbers_ever.add(pin["number"])
        self.rounds[pin["number"]] = pin["round"]
        return pin["number"]

    @rule(n=marks, action=st.sampled_from(USER_ACTIONS))
    def user_action(self, n, action):
        before = self.pin(n)
        if before is None:
            return
        try:
            after = self.ws.user_action(self.pid, n, action)
        except (service.RetouchError, store.StoreError):
            return  # refused transition: state must be unchanged (checked by invariants)
        self._note_transition(n, before["status"], after["status"], by_user=True)
        if action == "more_work":
            self.rounds[n] += 1

    @rule(n=marks, x=POS_PX, y=POS_PX, w=SIZE_PX, h=SIZE_PX)
    def move_mark(self, n, x, y, w, h):
        if self.pin(n) is None:
            return
        try:
            self.ws.update_pin(self.pid, n, geom={"x": x, "y": y, "w": w, "h": h})
        except (service.RetouchError, store.StoreError):
            return

    @rule(n=marks)
    def send(self, n):
        before = self.pin(n)
        if before is None:
            return
        try:
            self.ws.send(self.pid, [n])
        except service.RetouchError:
            return
        self._note_transition(n, before["status"], "queued", by_user=True)

    @rule(n=marks, with_pin=st.booleans())
    def revert(self, n, with_pin):
        before = self.pin(n)
        try:
            self.ws.revert(self.pid, pin=n if (with_pin and before) else None)
        except (service.RetouchError, store.StoreError):
            return
        if with_pin and before:
            self._note_transition(n, before["status"], "open", by_user=True)

    @rule(n=marks)
    def delete_mark(self, n):
        if self.pin(n) is None:
            return
        self.ws.delete_pin(self.pid, n)
        self.closed_by_user.discard(n)
        self.frozen_geom.pop(n, None)

    # -- agent rules ------------------------------------------------------------------------------
    @rule(n=marks, status=st.sampled_from(AGENT_STATUSES))
    def agent_sets_status(self, n, status):
        before = self.pin(n)
        if before is None:
            return
        with self.ws.db() as conn:
            try:
                after = store.agent_set_status(conn, self.pid, n, status)
            except store.StoreError:
                return
        self._note_transition(n, before["status"], after["status"], by_user=False)

    @rule(n=marks, value=st.floats(-1, 1), op=st.sampled_from(["exposure", "brightness", "redact"]))
    def agent_edits(self, n, value, op):
        before = self.pin(n)
        if before is None:
            return
        ops = [{"op": op, "value": "#00ff00" if op == "redact" else value}]
        try:
            result = self.ws.edit(self.pid, n, method="adjust", ops=ops)
        except (service.RetouchError, store.StoreError):
            return
        self.edit_outline[result.version_id] = (before["shape"], before["geom"])
        after = self.pin(n)
        if after is not None:
            self._note_transition(n, before["status"], after["status"], by_user=False)

    @rule(n=marks, msg=st.text(min_size=1, max_size=20), issues=st.text(max_size=20),
          status=st.sampled_from(AGENT_STATUSES))
    def agent_replies(self, n, msg, issues, status):
        before = self.pin(n)
        if before is None or not msg.strip():
            return
        try:
            self.ws.reply(self.pid, n, msg, status, issues=issues)
        except (service.RetouchError, store.StoreError):
            return
        after = self.pin(n)
        self._note_transition(n, before["status"], after["status"], by_user=False)

    # -- the promises -----------------------------------------------------------------------------
    @invariant()
    def p1_only_the_user_closes_a_mark(self):
        for p in self.project()["pins"]:
            if p["status"] in store.CLOSED:
                assert p["number"] in self.closed_by_user, f"#{p['number']} is {p['status']} but the user never closed it"
            else:
                assert p["number"] not in self.closed_by_user, f"#{p['number']} reopened without a user action"

    @invariant()
    def p2_a_sent_mark_keeps_its_outline(self):
        for p in self.project()["pins"]:
            if p["status"] != "open":
                frozen = self.frozen_geom.get(p["number"])
                assert frozen is not None, f"#{p['number']} is {p['status']} but the model never saw it leave open"
                assert repr(p["geom"]) == frozen, f"#{p['number']} moved while {p['status']}"

    @invariant()
    def p3_versions_form_a_tree_rooted_at_the_original(self):
        """Every version the model ever saw is still stored; the current version is reachable from
        the original by parent links; each child was added after its parent (seq grows along every
        edge). Edits after a revert branch off the reverted-to version, so this is a tree, not a line."""
        proj = self.project()
        versions = proj["versions"]
        by_id = {v["id"]: v for v in versions}
        assert self.versions_seen <= set(by_id), f"versions were deleted: {self.versions_seen - set(by_id)}"
        self.versions_seen |= set(by_id)
        assert proj["current_version"] in by_id
        assert len({v["seq"] for v in versions}) == len(versions), "two versions share a seq"
        roots = [v for v in versions if v["parent"] is None]
        assert len(roots) == 1 and roots[0]["seq"] == 0, "exactly one original, at seq 0"
        for v in versions:
            if v["parent"] is not None:
                assert v["parent"] in by_id, f"version {v['id']} has a parent outside the project"
                assert v["seq"] > by_id[v["parent"]]["seq"], f"version {v['id']} is older than its parent"
        cur = proj["current_version"]
        for _ in range(len(versions) + 1):
            if cur is None:
                break
            cur = by_id[cur]["parent"]
        assert cur is None, "the current version does not reach the original (parent cycle)"

    @invariant()
    def p4_every_version_changed_nothing_outside_its_mark(self):
        """Checked against the outline the mark had WHEN the edit was made (the model records it; the
        product stores only the pixel count), from the version files themselves."""
        proj = self.project()
        for v in proj["versions"]:
            if v["pin"] is None:
                continue
            assert v["outside_changed_px"] == 0
            outline = self.edit_outline.get(v["id"])
            assert outline is not None, f"version {v['id']} was not produced by an edit the model saw"
            shape, geom = outline
            parent, _ = self.ws.load(self.pid, v["parent"])
            after, _ = self.ws.load(self.pid, v["id"])
            mask = self.ws.mask({"shape": shape, "geom": geom}, parent.size)
            assert composite.outside_changed_px(parent, after, mask) == 0, (
                f"version {v['id']} for #{v['pin']} changed pixels outside the outline it was edited with")

    @invariant()
    def p5_round_only_grows_through_more_work(self):
        for p in self.project()["pins"]:
            assert p["number"] in self.rounds, f"#{p['number']} exists but the model never saw it created"
            assert p["round"] == self.rounds[p["number"]], f"#{p['number']} round drifted"


RetouchMachine.TestCase.settings = settings(max_examples=60, stateful_step_count=40, deadline=None)
TestRetouchStateMachine = RetouchMachine.TestCase
