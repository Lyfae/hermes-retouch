# Roadmap

Ideas for later, in the order they came up.

## Before/after diff view

Today the pane has hold-to-compare (whole image swaps to the original) and the agent gets a
side-by-side review crop. Add a third view: a diff of the current version against any earlier
one, with the changed pixels highlighted and everything else dimmed. The data is already there:
`composite.outside_changed_px` / `inside_changed_px` compute the per-pixel change for every
version, so the pane only needs a `GET .../versions/<a>/diff/<b>` route returning a PNG and a
slider or toggle in the header (Edited / Original / Diff).

Nice extras: show the changed-pixel count per mark in its bubble, and a per-version diff when
hovering a version in the undo history.

## Retouch a live web page → fix the code

Like Cursor's and Claude's browser annotation: open a page in the Desktop preview pane, draw a
mark on it, write a note, and Hermes changes the source so the page itself is fixed.

How it could work:

- A browser-page project kind next to the image kind. The "image" is a screenshot of the preview
  pane; each mark also records the DOM element(s) under it (selector, bounding box, computed
  styles, source location when a dev server exposes it via source maps or `data-source` attributes).
- `retouch_get` returns the marks with their DOM context; the agent edits the project's source
  files (normal file tools), the preview reloads, Retouch screenshots again and shows the
  before/after of the marked area in the mark's thread, same review loop as images.
- The pixel guarantee becomes a region guarantee: the edit is flagged if pixels OUTSIDE every
  mark changed after the reload, which catches a CSS change with wider blast radius than intended.

Caveats to check before building:

- Hermes Desktop already has preview-pane annotation (`preview-annotate/in-page.ts`). Catalog
  rule 8 forbids a plugin reaching into the app's own components, and rule 15 sends improvements
  to built-in features to core. The page kind must be built on the public plugin SDK (its own
  preview or a webview the plugin owns) or proposed as a core contribution.
- A screenshot → code change of a static image (option 2 from the scoping discussion) is a
  subset of this: the mark's note plus the image crop go to the agent, and the agent edits code
  instead of pixels. Same project model, no DOM context.
