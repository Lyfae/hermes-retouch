---
name: verify-retouch
description: "Use when changing retouch/composite.py, geometry.py, store.py, service.py or ops.py. Runs the property checks that prove the two promises Retouch makes."
version: 1.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [retouch, verification, hypothesis]
---

# Verify Retouch

Retouch makes two promises that tests alone cannot cover, because the cases that break them are
the ones nobody thinks to write:

1. **An edit never changes a pixel outside its mark.** If this breaks, a "remove the bag" edit
   quietly alters the person next to it, and the pixel proof the agent shows the user is a lie.
2. **The mark/version state machine is honest.** Only the user closes a mark; a sent mark keeps
   its outline; versions form a tree rooted at the original and are never deleted; every stored
   version is clean against the outline it was edited with; rounds grow only through "needs more
   work"; a mark number is never reused; Redo undoes Undo and moves only the picture.

`verify/` checks both with [Hypothesis](https://hypothesis.readthedocs.io): generated inputs
against the REAL compositing code, and generated sequences of user/agent calls against the REAL
store and service (SQLite in a temp dir). No model of the code exists separately from the code.

## When a change touches the verified code

| You changed… | Re-run | Then |
|---|---|---|
| `retouch/composite.py` (`apply_in_region`, `blend_full`, `inward_soft_mask`, `outside_changed_px`) | `verify/test_region_property.py` | nothing else unless it fails |
| `retouch/geometry.py` (`build_mask`, `brush_is_closed`, `validate`, `normalize`) | both files | a new shape needs a row in `geoms()` in test_region_property.py and in `add_mark` in test_state_machine.py |
| `retouch/ops.py` (`OPS`, `SOFT_EDGE_OPS`, `PRIVACY_OPS`, `run_ops`) | `verify/test_region_property.py` | numeric ranges are read from `OPS`; an op with no numeric range (like `redact`) needs its own strategy, as `redact` has in `test_adjust_in_region_...` |
| `retouch/store.py` (`USER_ACTIONS`, `AGENT_SETS`, `CLOSED`, `add_pin`, `add_version`, `revert`, `redo`, `redo_target`, `agent_set_status`, `user_action`, `update_pin`) | `verify/test_state_machine.py` | a new status or action: add it to the rules AND decide which promise it must keep (see "Updating the model") |
| `retouch/service.py` (`edit`, `_apply_and_save`, `update_pin`, `send`, `revert`, `redo`, `reply`) | `verify/test_state_machine.py` | same |

## Run

From the repo root (any machine with `uv`; no other install; `~/.hermes/bin/uv` on a Hermes
machine if it is not on PATH):

```bash
uv run --no-project --python 3.14 --with "pillow==13.0.0" --with pytest --with hypothesis pytest verify -q
```

About 10 s with a warm uv cache (up to a minute the first time). Python 3.14 and Pillow 13 are
what the Hermes gateway runs the plugin with; `tests/` also runs on 3.11 / Pillow 12.3, the
plugin's declared floor. Add `--hypothesis-show-statistics` to see how much of the input space a
run reached (the region check reports the share of marks touching the image edge and each shape's
share). `-p no:logging` hides the plugin's event-broadcast log lines, which have no gateway to
reach in a test process; a FAILED line is never hidden by it.

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
- revert: remove `status = 'open'` from `store.revert`'s pin update (caught by P2).

## Scope and bounds

Verified: images 24–96 px in RGB, RGBA and L; marks of every shape including ones hanging past the
image edge; every numeric op at any value in its declared range, plus `redact`; feathers 0–25 % of
the image; replacement content of arbitrary size; up to 40 user/agent steps per run, 60 runs, on
one project with rectangles and ellipses, `adjust` edits only.

Not verified: images larger than 96 px (the code is size-agnostic but the proof is bounded); the
`generate` and `from_file` edit methods at service level (their compositing is covered by the
region check with a stand-in generator, but crop, spill and download are not); chains of several
ops in one edit; `restore_status` after a failed edit (its `status = 'working'` guard matters
only with concurrent writers); concurrent callers in general (`add_version`'s `expected_parent`
conflict is reached only by the retry loop); screenshot, keep, discard and sweep (unit tests in
`tests/test_scratch.py` only); the pane's JavaScript; HEIC/EXIF handling (`tests/`).

## Coverage record

| Check | Covers |
|---|---|
| `verify/test_region_property.py` | `composite.apply_in_region`, `composite.blend_full`, `composite.inward_soft_mask`, `composite.outside_changed_px`, `geometry.build_mask`, `ops.run_ops` |
| `verify/test_state_machine.py` | `store.USER_ACTIONS/AGENT_SETS/CLOSED`, `store.add_pin`, `store.add_version`, `store.revert`, `store.redo`, `store.redo_target`, `store.agent_set_status`, `store.user_action`, `store.update_pin`, `service.edit`, `service._apply_and_save`, `service.update_pin`, `service.send`, `service.revert`, `service.redo`, `service.reply` |

The sync point is the last commit that touched `verify/`:

```bash
SYNC=$(git log -1 --format=%h -- verify/)
git log --oneline "$SYNC"..HEAD -- retouch/   # product changes the verification has not been re-read against
```

If that prints anything, re-read the covered functions in that diff against the rules and
invariants, re-run, do one "proving the check still bites" break, and then touch `verify/` in your
commit (even a docstring line) so the sync point moves forward.
