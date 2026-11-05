---
name: retouch
description: Work a Retouch image project mark by mark.
---

# Retouch

The user opened an image in the Retouch pane, drew numbered marks on the areas that need work, and
wrote a note on each. Every mark has its own thread. Your job is to fix only what they marked, show
them, and let them decide.

## Loop

1. `retouch_get(project_id)` first. Work the marks the request names; by default every `open` or
   `queued` mark. Read each mark's note and its thread: a later user message in the thread overrides
   the original note (round 2+ means they asked for more work). Look at each close-up: only pixels
   inside the outline can change. If the thing to fix runs past the outline (a shadow, a bag handle),
   say so: `status="question"` asking for a bigger mark, or edit and report what will remain.
2. Pick a method per mark:
   - `ask`: no edit. Answer in `retouch_reply` (status `question` if you need their input, otherwise
     `needs_review`).
   - `fix` / `enhance`: `method="adjust"` with ops. Start gentle (exposure 0.3–0.7, contrast 0.1–0.3);
     one clear change beats five subtle ones.
   - `remove` / replace: `method="generate"`. The prompt defaults to the note; rewrite it only to be
     more concrete about what should be there instead ("grass that matches the lawn around it").
   - Anything you computed yourself on a full-size copy of the current image (for example with code):
     `method="from_file"` with that file's absolute path. Only the marked area is taken from it.
3. Look at the before/after the tool returns. Does it satisfy the note, with no leftover trace, halo,
   seam or texture that does not match? If not, try once or twice more with a different approach
   (other ops, stronger values, a more specific prompt), each a new version. If the result carries
   `edge_check`, the object continues past the outline: retrying cannot fix that part, so report it
   and ask for a bigger mark instead.
4. `retouch_reply(status="needs_review", version=..., remaining_issues=...)` in one or two plain
   sentences: what you changed. `remaining_issues` is shown to the user as a warning: name exactly
   what is still visibly wrong, `""` only if nothing is. Never call an edit clean, in the thread or
   in chat, when you saw a flaw. If you could not do it, `status="question"` and say what you need.
5. Never say a mark is resolved or accepted. The user accepts, sends it back, or reverts in the pane.
6. Export only when asked: `retouch_export`, then put the returned `MEDIA:` line in your reply.

## Notes

- `generate` needs an image generation provider configured in `hermes tools`. If it errors, say so in
  the thread and offer the `adjust` alternative where one exists.
- Each edit is a new version; nothing is lost, so trying again is cheap. Paid generations are not:
  don't regenerate more than twice per mark without the user asking.
