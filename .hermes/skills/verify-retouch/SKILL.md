---
name: verify-retouch
description: "Use when changing retouch/composite.py, geometry.py, store.py, service.py, ops.py or the parallel Send in desktop/plugin.js. Runs the checks that prove the promises Retouch makes."
version: 1.2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [retouch, verification, hypothesis]
---

# Verify Retouch

Retouch makes four promises that tests alone cannot cover, because the cases that break them are
the ones nobody thinks to write:

1. **An edit never changes a pixel outside its mark.** If this breaks, a "remove the bag" edit
   quietly alters the person next to it, and the pixel proof the agent shows the user is a lie.
2. **The mark/version state machine is honest.** Only the user closes a mark; a sent mark keeps
   its outline; versions form a tree rooted at the original and are never deleted; every stored
   version is clean against the outline it was edited with; rounds grow only through "needs more
   work"; a mark number is never reused; Redo undoes Undo and moves only the picture; a mark's
   Revert takes out that mark's own edit and nothing else.

3. **Edits made at the same moment all land.** Send runs up to three agent sessions on one
   picture at once. If this breaks, one session saves a picture built before another's edit and
   silently wipes that edit out, or leaves a half-saved version behind.
4. **The pane's Send queue never loses or doubles work.** At most three sessions run, overlapping
   marks sent together stay in one session, a mark is never held by two sessions, and nothing is
   left "Sent" with nobody working on it. If this breaks, marks hang forever or a late event from
   an old session takes a new request away.

`verify/` checks 1–3 with [Hypothesis](https://hypothesis.readthedocs.io): generated inputs
against the REAL compositing code, generated sequences of user/agent calls against the REAL store
and service (SQLite in a temp dir), and real threads editing one project together. Promise 4 is
checked by `verify/pane_scheduler.mjs`: seeded random schedules against the REAL scheduler code
cut out of `desktop/plugin.js`. No model of the code exists separately from the code.

## When a change touches the verified code

| You changed… | Re-run | Then |
|---|---|---|
| `retouch/composite.py` (`apply_in_region`, `blend_full`, `inward_soft_mask`, `outside_changed_px`) | `verify/test_region_property.py` | nothing else unless it fails |
| `retouch/geometry.py` (`build_mask`, `brush_is_closed`, `validate`, `normalize`) | both files | a new shape needs a row in `geoms()` in test_region_property.py and in `add_mark` in test_state_machine.py |
| `retouch/ops.py` (`OPS`, `SOFT_EDGE_OPS`, `PRIVACY_OPS`, `run_ops`) | `verify/test_region_property.py` | numeric ranges are read from `OPS`; an op with no numeric range (like `redact`) needs its own strategy, as `redact` has in `test_adjust_in_region_...` |
| `retouch/store.py` (`USER_ACTIONS`, `AGENT_SETS`, `CLOSED`, `add_pin`, `add_version`, `revert`, `redo`, `redo_target`, `agent_set_status`, `user_action`, `update_pin`) | `verify/test_state_machine.py` | a new status or action: add it to the rules AND decide which promise it must keep (see "Updating the model") |
| `retouch/service.py` (`edit`, `_apply_and_save`, `update_pin`, `send`, `revert`, `redo`, `reply`) | `verify/test_state_machine.py` | same |
| `service._apply_and_save` (the retry loop, the unlink on conflict), `service.edit` (status restore), `store.add_version` (`expected_parent`, the `result_version` update), `service.load` | `verify/test_parallel_edits.py` | changing the number of attempts changes P14 and the "worst schedule" count: update `Staged.stage` and the last two assertions together |
| `desktop/plugin.js`: `overlapGroups`, `startJob`, `pump`, `finish`, `settle`, `held`, `ownTurnEnded`, `sendToAgent`, `MAX_PARALLEL`, `inFlight` / `waiting` / `launching` | `node verify/pane_scheduler.mjs` | if it says it cannot find the scheduler, the code moved: update the four `cut(...)` points at the top of the script, nothing else |

## Run

From the repo root (any machine with `uv`; no other install; `~/.hermes/bin/uv` on a Hermes
machine if it is not on PATH):

```bash
uv run --no-project --python 3.14 --with "pillow==13.0.0" --with pytest --with hypothesis pytest verify -q
node verify/pane_scheduler.mjs      # the pane's Send queue; needs only Node, about a second
```

The first command takes about 15 s with a warm uv cache (up to a minute the first time). Python
3.14 and Pillow 13 are what the Hermes gateway runs the plugin with; `tests/` also runs on 3.11 /
Pillow 12.3, the plugin's declared floor. Add `--hypothesis-show-statistics` to see how much of the
input space a run reached (the region check reports the share of marks touching the image edge and
each shape's share; the parallel check reports, per run, how many sessions ran and how many edits
were told to try again, on the "worst schedule" and "free running"). `-p no:logging` hides the
plugin's event-broadcast log lines, which have no gateway to reach in a test process; a FAILED
line is never hidden by it.

## Reading a failure

Hypothesis prints the SHORTEST sequence it found, as calls on `state`:

```
marks_0 = state.add_mark(h=4, intent='fix', shape='rect', w=4, x=0, y=0)
state.agent_edits(n=marks_0, op='exposure', value=1.0)
state.delete_mark(n=marks_0)
marks_1 = state.add_mark(...)        # <- the step after which an invariant failed
```

Read it as the user's and agent's actions in order. The failing invariant's NAME says which
promise broke (`p1_only_the_user_closes_a_mark`, …, listed in the file's docstring); the assertion
message names the mark or version in the code's own terms. Decide:

- **A real bug** when the sequence is something a user and the agent can actually do through the
  pane and the tools. Fix the product code; the check is the regression test. Do not weaken the
  invariant to make it pass. (This check found one: mark numbers were reused after a delete, so a
  version's `pin` pointed at a different mark.)
- **A modeling error** when the model's oracle (`closed_by_user`, `frozen_geom`, `edit_outline`,
  `numbers_ever`, `versions_seen`, `rounds`) recorded the wrong thing. Fix the rule that updated
  it. Known shape of this mistake: the oracle recording a transition the product refused, or
  recording an agent transition as the user's.
- **A changed promise** when the user asked for the behaviour the check rejects. Change the
  invariant AND the docstring line for that promise in the same commit, and say so in the commit.

For the parallel check (`test_parallel_edits.py`), the failure prints the marks and edits and
whether the run was `staged`. `staged=True` is the worst schedule: every session reads the picture
before any saves, on each attempt, so the run is the same every time. `staged=False` lets the
threads race, so a failure there may not repeat; re-run it, and trust a `staged=True` failure
first. The assertion message says which promise broke in plain words ("changed N pixels outside
its mark (another mark's work)" is a lost edit).

For the pane check, the failure prints the promise (S1–S4), the seed, and the last steps in order
("Send #1,#5", "session.create failed", "agent in s2 finishes on #3", "event error for s1",
"refresh"). Read them as what the user, the gateway and the agents did. Before calling it a bug,
check the step order is one the pane allows: the user acts only on what a refresh has shown, and a
turn-ended event exists only for a session whose prompt was accepted.

For the region check, the failure prints the image size, shape, geometry and edit that leaked;
`outside_is_untouched` compares every band exactly, so a 1-pixel, 1-level leak is a failure.

## Updating the model

- New **user action** (`USER_ACTIONS`): `user_action` picks it up automatically and records it with
  `by_user=True`; `_note_transition` handles closing and reopening.
- New **agent status** (`AGENT_SETS`): picked up automatically. It must never be in `CLOSED`.
- New **edit method**: add a rule that calls `ws.edit(..., method=...)` and records
  `self.edit_outline[result.version_id] = (before["shape"], before["geom"])`, the same as
  `agent_edits`. Generative methods need a stubbed `generator`.
- New **shape**: a geometry strategy in both files, and `build_mask` must raise `GeometryError`
  (not return an empty mask) for a shape covering no pixel.
- Changing a **promise** is a product decision; record it in README "What it does" too.
- New **way work can start or end in the pane** (another fallback, a cancel button, a new event):
  add it as an action in `run()` in `pane_scheduler.mjs` and decide which of S1–S4 it must keep.
- `MAX_PARALLEL` in `desktop/plugin.js` and `PANE_LIMIT` in `test_parallel_edits.py` are the same
  number. Raising it above the three attempts in `_apply_and_save` breaks P14 on purpose: more
  sessions than attempts means an edit can be told to try again.

## Proving the check still bites

A check nobody has seen fail proves nothing. After touching `verify/`, break the product once and
watch the right file fail, then restore it (`git checkout retouch/`). Each of these has been run
and fails as stated:

- region: in `composite.inward_soft_mask`, drop the final `ImageChops.multiply(..., hard)` → three
  region tests fail.
- P1 (closing): add `"resolved"` to `store.AGENT_SETS`.
- P1 (reopening): in `store.agent_set_status`, make the `in CLOSED` refusal `False`.
- P2: in `service.update_pin`, make the `status != "open"` check `False`.
- P3: in `store.revert`, also `DELETE FROM versions WHERE seq > <reverted-to seq>`.
- P7: in `store.add_pin`, replace `row["last_pin"]` with `0`.
- P8 (which edit): in `store.redo_target`, order by `seq ASC` (the slowest break to find, about a
  minute: it needs two edits built on one version).
- P8 (only when undone): in `store.redo_target`, replace `AND parent = ?` with `AND id != ?`.
- P8 (marks untouched): in `store.redo`, also `UPDATE pins SET round = round + 1`.
- P9 (reopens): remove `status = 'open'` from `store.revert`'s pin update.
- P9 (own edit only): in `store.revert`, make the `result_version != current["id"]` refusal `False`.
- P10/P11 (no lost edit): in `store.add_version`, make the `expected_parent` refusal `if False:`.
- P12 (mark points at its edit): in `store.add_version`, drop the `UPDATE pins SET result_version`.
- P13 (nothing left behind): in `service._apply_and_save`, drop the `unlink` in the
  `except store.VersionConflict` branch.
- P14 (three at once always land): in `service._apply_and_save`, `range(3)` → `range(2)`.

For the pane, break a COPY and point the script at it (`cp desktop/plugin.js /tmp/x.js`, edit,
`node verify/pane_scheduler.mjs /tmp/x.js`); each fails as stated:

- S1: in `pump`, drop `+ launching.size` from the limit test.
- S1 (double count): in `startJob`, delete the `launching.delete(job)` line.
- S2: in `settle`, keep every mark until none is busy instead of letting go mark by mark.
- S3: in `overlapGroups`, replace the `hit` filter with `[]`.
- S4 (queue stalls): in `finish`, do not call `pump()`.
- S4 (mark left "Sent"): in `startJob`, drop the `unsend` after a failed fallback.

## Scope and bounds

Verified: images 24–96 px in RGB, RGBA and L; marks of every shape including ones hanging past the
image edge; every numeric op at any value in its declared range, plus `redact`; feathers 0–25 % of
the image; replacement content of arbitrary size; up to 40 user/agent steps per run, 60 runs, on
one project with rectangles and ellipses, `adjust` edits only. Parallel edits: 2–6 sessions, one
`adjust` edit each, on rectangles that may overlap, in one process, both free running and on the
worst schedule (40 runs). Pane queue: 400 seeded schedules of 70 steps over 8 marks, with gateway
calls answered late, out of order or refused, agents finishing, failing or stopping half way, and
events late, doubled or for other sessions.

Not verified: images larger than 96 px (the code is size-agnostic but the proof is bounded); the
`generate` and `from_file` edit methods at service level (their compositing is covered by the
region check with a stand-in generator, but crop, spill and download are not); chains of several
ops in one edit; `restore_status` after a failed edit (its `status = 'working'` guard matters
only with concurrent writers, and the parallel check covers only the give-up path); a session
that makes several edits while others run (it can be told to try again; P13 says that is clean);
two gateway processes on one data folder; that the gateway really delivers a turn-ended event for
every hidden session (the pane check assumes it; `settle` is the fallback); what the hidden
sessions' agents do beyond calling the four tools; pasted (scratch) projects, keep, discard and sweep (unit tests in
`tests/test_scratch.py` only); what a steered agent or a hostile image provider may reach (files
outside the project, private network addresses, oversized input: example-based tests in
`tests/test_hardening.py`, not generated ones); the rest of the pane's JavaScript (drawing,
clipboard, model picker, banner, version strip); HEIC/EXIF handling (`tests/`).

## Coverage record

| Check | Covers |
|---|---|
| `verify/test_region_property.py` | `composite.apply_in_region`, `composite.blend_full`, `composite.inward_soft_mask`, `composite.outside_changed_px`, `geometry.build_mask`, `ops.run_ops` |
| `verify/test_parallel_edits.py` | `service.edit`, `service._apply_and_save`, `service.load`, `service.mask`, `store.add_version`, `store.restore_status`, `composite.outside_changed_px` |
| `verify/pane_scheduler.mjs` | `desktop/plugin.js`: `bounds`, `overlapGroups`, `startJob`, `pump`, `finish`, `settle`, `held`, `ownTurnEnded`, `sendToAgent` |
| `verify/test_state_machine.py` | `store.USER_ACTIONS/AGENT_SETS/CLOSED`, `store.add_pin`, `store.add_version`, `store.revert`, `store.redo`, `store.redo_target`, `store.agent_set_status`, `store.user_action`, `store.update_pin`, `service.edit`, `service._apply_and_save`, `service.update_pin`, `service.send`, `service.revert`, `service.redo`, `service.reply` |

The sync point is the last commit that touched `verify/`:

```bash
SYNC=$(git log -1 --format=%h -- verify/)
git log --oneline "$SYNC"..HEAD -- retouch/ desktop/plugin.js   # product changes the verification has not been re-read against
```

If that prints anything, re-read the covered functions in that diff against the rules and
invariants, re-run, do one "proving the check still bites" break, and then touch `verify/` in your
commit (even a docstring line) so the sync point moves forward.
