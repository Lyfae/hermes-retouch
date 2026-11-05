# Retouch

Mark the parts of an image that need work, leave a note on each mark, and Hermes fixes only those
areas. Every edit is checked pixel by pixel to change nothing outside its mark, saved as a new
version, and reviewed by you in that mark's own thread.

Retouch is a Hermes plugin with two halves: a pane in the Hermes Desktop app where you draw and
review, and four agent tools Hermes uses to do the work.

## What it does

- **Marks.** Draw with the pen, a box, an ellipse or a lasso, or drop a note at a spot. Each mark
  has an intent (fix, remove, enhance, ask) and a note in your own words.
- **One thread per mark.** Send the marks and Hermes works on them in the chat next to the pane.
  Its replies, questions and your answers stay attached to the mark they are about.
- **Edits stay inside the marks.** Every result is blended through the mark's mask and compared
  with the previous version. An edit that changes even one pixel outside its mark is refused and
  never saved.
- **Three ways to edit.**
  - `adjust`: local tonal and privacy operations (exposure, brightness, contrast, saturation,
    warmth, grayscale, sharpen, denoise, blur, pixelate, redact). Tonal changes fade toward the
    mark's edge so no outline shows; privacy operations cover every pixel of the mark.
  - `generate`: repaints the marked area with your configured image model, for removing or
    replacing things.
  - `from_file`: takes a full-size copy the agent edited another way and keeps only the marked area.
- **Honest review.** Hermes has to say what is still visibly wrong after each edit, and the pane
  shows it as "Still visible: …". When a removed object continues past the edge of its mark, the
  edge check says so whatever Hermes reports, and suggests drawing a bigger mark.
- **Versions.** Each edit is a new version. In a mark's bubble you can Accept it, ask for more
  work, Revert it, or close it as won't fix. Undo the last edit from the header, and hold the eye
  button to compare with the original.
- **Save.** PNG, JPEG or WebP, written as a new file. Your original is never modified. GPS data is
  stripped and the color profile is kept.

## Requirements

- Hermes `>= 0.22.7` and the Hermes Desktop app. Images are opened and marked in the pane.
- For removals and replacements (`generate`): an image generation provider configured in Hermes.
  Tonal fixes and privacy edits work without one.
- HEIC photos open when `pillow-heif` is installed (Hermes ships it on Python 3.14 and newer).

## Install

From the catalog, once listed:

```bash
hermes plugins install retouch
```

From source:

```bash
git clone https://github.com/Lyfae/hermes-retouch
ln -s "$PWD/hermes-retouch" ~/.hermes/plugins/retouch
hermes plugins enable retouch
```

Then turn on the Desktop half in the app: **Capabilities → Plugins → Retouch**. The pane joins the
right side of the window; drag it wherever you like.

After updating the plugin, restart the gateway (`hermes gateway restart`). The pane's backend
routes are loaded once per gateway process.

## Using it

1. Open an image in the Retouch pane: the button, drag and drop, or paste with ⌘V.
2. Pick a tool from the toolbar: Select **V**, Pen **P**, Box **R**, Ellipse **E**, Lasso **L**,
   Note **T**. Draw on the image, choose an intent, write what you want, and press ⌘↩.
3. Click **Send**. The pane asks Hermes in the open chat to work on your marks.
4. Click a mark's number to see Hermes's reply, the result and any warnings. Accept it, or say what
   is still wrong and send it back.
5. **Save ▾** writes the finished image.

Open marks can be moved with Select; once sent, an edit belongs to that outline.

## Agent tools

| Tool | What it does |
| --- | --- |
| `retouch_get` | Reads a project: its marks, notes, threads and versions. |
| `retouch_edit` | Edits one mark with `adjust`, `generate` or `from_file`, and returns the pixel proof, the edge check and a before/after review image. |
| `retouch_reply` | Posts Hermes's reply on a mark, with its status and what is still visibly wrong. |
| `retouch_export` | Writes the current version to the project's exports folder. |

Only you close a mark. Hermes can mark it as working, ready for review, or a question for you.

## Data, network and privacy

- Projects, versions, crops, review images and exports live in
  `<HERMES_HOME>/plugin-data/retouch/`, separately for each Hermes profile.
- `generate` sends a crop of the area around the mark (not the whole image) to Hermes's
  `image_generate` tool, which forwards it to the image provider you configured. Retouch then
  downloads the result from the URL the provider returns. Your provider's own data retention
  applies; some providers return results at public URLs, so don't use `generate` on images you
  can't share with your provider.
- `from_file` reads an image at an absolute path the agent passes to it.
- Retouch makes no other network calls, runs no shell commands or background processes, stores no
  credentials and sends no telemetry.

## Development

```bash
uv run --no-project --python 3.11 --with "pillow==12.3.0" --with pytest pytest tests -q
hermes plugins validate .
```

The library lives in `retouch/` (geometry, compositing, operations, storage, the service), the
agent tools in `tools.py`, the pane's REST backend in `dashboard/plugin_api.py` and the pane in
`desktop/plugin.js`.

## License

MIT
