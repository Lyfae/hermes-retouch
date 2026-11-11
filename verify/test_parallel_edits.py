"""Concurrency check: several agent sessions edit ONE project at the same moment, as the pane's
parallel Send makes them do, on the real service and store (SQLite in a temp dir, real threads
released together by a barrier). Hypothesis generates the marks (overlapping or not), the edits
and how many sessions run at once.

Promises checked after every run:
  P10  No edit is lost or overwritten. The final picture is exactly what the same successful edits
       give when applied one after another in the order their versions were saved.
  P11  Every saved version differs from its parent only inside its own mark, checked on the version
       files (an edit built on a stale picture and saved anyway would carry the stale pixels of
       another mark with it, outside its own).
  P12  The versions form one unbroken chain: each edit's parent is the edit saved just before it,
       and each mark points at its own newest edit.
  P13  An edit that gives up ("the image kept changing") leaves nothing behind: no version row, no
       version file, and the mark back at the status it had.
  P14  With at most three edits at once (the pane's limit) and one edit per session, none gives up.

Not checked here: the generate and from_file methods (adjust only); more than one process (the
gateway runs every session in one process); the pane's own scheduling (verify/pane_scheduler.mjs).

Run:  uv run --no-project --python 3.14 --with "pillow==13.0.0" --with pytest --with hypothesis \\
        pytest verify -q
"""
from __future__ import annotations

import io
import random
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import HealthCheck, event, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from retouch import composite, service, store  # noqa: E402

W, H = 64, 48
PANE_LIMIT = 3  # MAX_PARALLEL in desktop/plugin.js


def noise_png(seed):
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", (W, H), bytes(rnd.getrandbits(8) for _ in range(W * H * 3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


RECT = st.fixed_dictionaries({"x": st.integers(0, W - 8), "y": st.integers(0, H - 8),
                              "w": st.integers(6, 30), "h": st.integers(6, 30)})
# Values far enough from zero that the edit changes pixels on noise (an edit that changes nothing
# inside its mark is refused, which is a different promise).
EDIT = st.tuples(st.sampled_from(["exposure", "contrast", "saturation"]),
                 st.one_of(st.floats(0.4, 1.0), st.floats(-1.0, -0.4)))
JOBS = st.lists(st.tuples(RECT, EDIT), min_size=2, max_size=6)


def same(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and a.mode == b.mode and ImageChops.difference(a, b).getbbox() is None


class Staged(service.Workspace):
    """The real workspace on the worst schedule: every session still trying reads the picture
    before any of them saves, on each of its attempts. Only timing changes; no product step is
    replaced. Exactly one save wins each round (the store takes them one at a time), so attempt k
    is made by `sessions - k + 1` sessions, and that many wait for each other here."""

    def stage(self, sessions):
        self._gates = [threading.Barrier(sessions - k) for k in range(min(3, sessions))]
        self._attempt = threading.local()

    def load(self, project_id, version_id=None):
        gates = getattr(self, "_gates", None)
        if gates and threading.current_thread() is not threading.main_thread():
            k = getattr(self._attempt, "n", 0)
            self._attempt.n = k + 1
            if k < len(gates):
                picture = super().load(project_id, version_id)
                gates[k].wait(20)  # everyone has read the same picture; now they all try to save
                return picture
        return super().load(project_id, version_id)


def project_with_marks(root, seed, jobs, kind=service.Workspace):
    ws = kind(Path(root))
    pid = ws.create_project("photo.png", noise_png(seed))["id"]
    numbers = [ws.add_pin(pid, shape="rect", geom=rect, units="px", intent="fix", note="x")["number"] for rect, _ in jobs]
    ws.send(pid)
    return ws, pid, numbers


def ops_of(edit):
    return [{"op": edit[0], "value": edit[1]}]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2**16), jobs=JOBS, staged=st.booleans())
def test_edits_made_at_the_same_moment_all_land_and_none_overwrites_another(seed, jobs, staged):
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as replay_root:
        ws, pid, numbers = project_with_marks(root, seed, jobs, Staged if staged else service.Workspace)
        if staged:
            ws.stage(len(jobs))
        go = threading.Barrier(len(jobs))
        done: dict[int, object] = {}

        def session(number, edit):
            go.wait()
            try:
                done[number] = ws.edit(pid, number, method="adjust", ops=ops_of(edit))
            except Exception as exc:  # judged below: only "kept changing" is an allowed outcome
                done[number] = exc

        threads = [threading.Thread(target=session, args=(n, edit)) for n, (_, edit) in zip(numbers, jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "an edit never returned"

        gave_up = {n for n, r in done.items() if isinstance(r, Exception)}
        for n in gave_up:
            assert isinstance(done[n], service.RetouchError) and "kept changing" in str(done[n]), \
                f"mark #{n}: an edit made at the same moment as others failed with {done[n]!r}"
        event(f"{'worst schedule' if staged else 'free running'}: sessions={len(jobs)} gave_up={len(gave_up)}")

        proj = ws.project(pid)
        versions = sorted(proj["versions"], key=lambda v: v["seq"])
        pins = {p["number"]: p for p in proj["pins"]}
        landed = [n for n in numbers if n not in gave_up]

        # P12: one chain, in save order, and each mark points at its own edit
        assert len(versions) == 1 + len(landed), "the number of versions is not the original plus one per edit that landed"
        for parent, child in zip(versions, versions[1:]):
            assert child["parent"] == parent["id"], f"version {child['id']} was not built on the one saved before it"
        assert proj["current_version"] == versions[-1]["id"]
        for n in landed:
            assert pins[n]["result_version"] == done[n].version_id
            assert next(v for v in versions if v["id"] == done[n].version_id)["pin"] == n

        # P11: each saved file differs from its parent's file only inside its own mark
        files = {v["id"]: Image.open(ws.version_path(pid, v["id"])).copy() for v in versions}
        for parent, child in zip(versions, versions[1:]):
            mask = ws.mask(pins[child["pin"]], (W, H))
            leaked = composite.outside_changed_px(files[parent["id"]], files[child["id"]], mask)
            assert leaked == 0, f"mark #{child['pin']}'s edit changed {leaked} pixels outside its mark (another mark's work)"

        # P13: an edit that gave up left no file and put its mark back
        on_disk = {p.name for p in (ws.project_dir(pid) / "versions").iterdir()}
        assert on_disk == {ws.version_path(pid, v["id"]).name for v in versions}, "a version file exists that no version refers to"
        for n in gave_up:
            assert pins[n]["status"] == "queued" and pins[n]["result_version"] is None, f"mark #{n} gave up but is {pins[n]['status']}"

        # P10: the same edits, one at a time in save order, give the same picture
        calm, calm_pid, calm_numbers = project_with_marks(replay_root, seed, jobs)
        number_of = dict(zip(numbers, calm_numbers))
        edit_of = {n: edit for n, (_, edit) in zip(numbers, jobs)}
        for v in versions[1:]:
            calm.edit(calm_pid, number_of[v["pin"]], method="adjust", ops=ops_of(edit_of[v["pin"]]))
        assert same(files[versions[-1]["id"]], Image.open(calm.version_path(calm_pid))), \
            "the picture after edits made at the same moment is not what the same edits give one after another"

        # P14, last so that a lost edit is reported as a lost edit and not as a miscount
        if len(jobs) <= PANE_LIMIT:
            assert not gave_up, f"with {len(jobs)} edits at once (the pane allows {PANE_LIMIT}), marks {sorted(gave_up)} gave up"
        if staged:
            # three attempts each, one winner per round: the rest are told to try again
            assert len(gave_up) == max(0, len(jobs) - 3), f"{len(gave_up)} of {len(jobs)} edits gave up on the worst schedule"
