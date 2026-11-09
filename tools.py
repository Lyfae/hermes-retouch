"""The four agent tools. Handlers return JSON strings, or the multimodal envelope when the model
should see pixels (non-vision models get ``text_summary`` instead; Hermes strips the images)."""

from __future__ import annotations

import json
from typing import Any

from .retouch import geometry, ops as ops_mod, render, service, store

TOOLSET = "retouch"
_ERRORS = (service.RetouchError, store.StoreError, geometry.GeometryError)

HOW_TO_WORK = (
    "How to work a Retouch project: handle the marks named in the request (default: every open or queued "
    "mark). Before editing, look at each close-up: only pixels inside the outline can change, so if the thing "
    "to fix continues past the outline, say so with retouch_reply(status='question') and ask the user to draw "
    "a bigger mark (or edit and report what will remain). intent=ask: answer in retouch_reply, no edit. "
    "fix/enhance: prefer method='adjust'. remove/replace: method='generate'. An edit you computed yourself on "
    "a full-size copy of the image: method='from_file'. After each retouch_edit, compare before and after "
    "with the note; a leftover trace, halo, seam or mismatched texture is not acceptable: retry with a "
    "different approach (at most 2 retries). Then retouch_reply(status='needs_review') in one or two "
    "sentences, with remaining_issues set to exactly what is still visibly wrong ('' only if nothing is). "
    "Never call an edit clean if you saw a flaw, here or in your chat answer. Never claim a mark is "
    "resolved: only the user accepts. Export only when the user asks."
)

GET = {
    "name": "retouch_get",
    "description": (
        "Open a Retouch image project: the user's image with numbered marks they drew, each with a note "
        "and a comment thread. Without project_id, lists projects. With project_id, returns the project "
        "state, an overview with every mark outlined and numbered, and a close-up of each requested mark."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project id (rt_...). Omit to list projects."},
            "pins": {"type": "array", "items": {"type": "integer"},
                     "description": "Mark numbers to show close-ups of. Default: every open mark."},
        },
    },
}

EDIT = {
    "name": "retouch_edit",
    "description": (
        "Edit ONE marked area of a Retouch project. Only pixels inside the mark can change; the result is "
        "proved (0 pixels outside the mark changed) and saved as a new version, so the user can compare "
        "and revert. Returns before/after close-ups. Methods: 'adjust' applies ops locally with no API key: "
        f"{ops_mod.describe()}. 'generate' asks the image generation provider configured in Hermes to "
        "repaint the marked area from a prompt (remove or replace things). 'from_file' takes the marked "
        "area from a full-size edited copy of the current image that you produced yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "pin": {"type": "integer", "description": "Mark number."},
            "method": {"type": "string", "enum": list(service.METHODS)},
            "ops": {
                "type": "array",
                "description": "For method=adjust: applied in order.",
                "items": {"type": "object", "properties": {
                    "op": {"type": "string", "enum": list(ops_mod.OPS)},
                    "value": {"type": ["number", "string"], "description": "Strength; a color for redact."}},
                    "required": ["op"]},
            },
            "prompt": {"type": "string", "description": "For method=generate: what the marked area should "
                       "show. Defaults to the mark's note."},
            "file": {"type": "string", "description": "For method=from_file: absolute path to a full-size "
                     "edited copy of the current version (same width and height)."},
        },
        "required": ["project_id", "pin", "method"],
    },
}

REPLY = {
    "name": "retouch_reply",
    "description": (
        "Post your reply in a mark's thread and set its status: 'needs_review' when the user should look "
        "at your edit, 'question' when you need something from them, 'working' for a progress note. Only "
        "the user can accept or close a mark. remaining_issues is shown to the user as a warning: state "
        "honestly what is still visibly wrong (a leftover trace, a seam, part of the object outside the "
        "mark), or '' only when nothing is."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "pin": {"type": "integer"},
            "message": {"type": "string", "description": "One or two plain sentences."},
            "status": {"type": "string", "enum": sorted(store.AGENT_SETS)},
            "remaining_issues": {"type": "string", "description": "What is still visibly wrong; '' if nothing."},
            "version": {"type": "string", "description": "The version id your edit produced, if any."},
        },
        "required": ["project_id", "pin", "message", "status", "remaining_issues"],
    },
}

EXPORT = {
    "name": "retouch_export",
    "description": (
        "Save a Retouch project's image as a NEW file (never overwrites; the original is never touched). "
        "GPS location is removed by default and the color profile is kept. Use only when the user asks to "
        "save or export. Put the returned media line in your reply so the user gets the file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version": {"type": "string", "description": "Version id. Default: the current version."},
            "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
            "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 90},
            "max_side": {"type": "integer", "minimum": 64, "description": "Downscale so the longer side fits."},
            "strip_gps": {"type": "boolean", "default": True},
            "directory": {"type": "string", "description": "Absolute folder to save into. Default: the "
                          "project's exports folder."},
        },
        "required": ["project_id"],
    },
}


def _ok(**data: Any) -> str:
    return json.dumps({"success": True, **data})


def _err(exc: Exception) -> str:
    return json.dumps({"success": False, "error": str(exc)})


def _image(img) -> dict:
    return {"type": "image_url", "image_url": {"url": render.data_url(img)}}


def _envelope(summary: dict, parts: list[dict]) -> dict:
    text = json.dumps(summary)
    return {"_multimodal": True, "content": [{"type": "text", "text": text}, *parts], "text_summary": text}


def _pin_brief(pin: dict) -> dict:
    return {k: pin[k] for k in ("number", "intent", "note", "status", "round", "result_version")} | {
        "thread": [{"author": m["author"], "body": m["body"]} | ({"remaining_issues": m["issues"]} if m.get("issues") else {})
                   for m in pin["thread"][-6:]]}


def handle_get(args: dict, **_kw) -> str | dict:
    ws = service.Workspace.current()
    pid = args.get("project_id")
    try:
        if not pid:
            return _ok(projects=ws.projects())
        pins = [int(n) for n in args.get("pins") or []] or None
        proj, overview, crops = ws.views(pid, pins)
    except _ERRORS as exc:
        return _err(exc)
    summary = {"success": True, "project_id": proj["id"], "title": proj["title"],
               "size": [proj["width"], proj["height"]], "current_version": proj["current_version"],
               "marks": [_pin_brief(p) for p in proj["pins"]], "how_to_work": HOW_TO_WORK}
    parts = [{"type": "text", "text": "Overview, marks outlined and numbered:"}, _image(overview)]
    for n, crop in crops:
        parts += [{"type": "text", "text": f"Close-up of mark #{n} (outline shows the mark):"}, _image(crop)]
    return _envelope(summary, parts)


def make_generator(ctx):
    def generate(crop_path, prompt):
        raw = ctx.dispatch_tool("image_generate", {"prompt": prompt, "image_url": str(crop_path)})
        try:
            res = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            res = None
        if not isinstance(res, dict) or not res.get("success") or not res.get("image"):
            reason = res.get("error") if isinstance(res, dict) else None
            raise service.RetouchError(f"image generation failed: {reason or 'no image returned'}")
        return service.load_image_ref(res["image"])
    return generate


def make_edit_handler(ctx):
    def handle_edit(args: dict, **_kw) -> str | dict:
        ws = service.Workspace.current()
        try:
            result = ws.edit(str(args.get("project_id") or ""), int(args.get("pin") or 0),
                             method=str(args.get("method") or ""), ops=args.get("ops"), prompt=args.get("prompt"),
                             file=args.get("file"), generator=make_generator(ctx))
        except _ERRORS as exc:
            return _err(exc)
        strip = render.before_after(result.before, result.after)
        review = ws.project_dir(result.project_id) / "reviews" / f"{result.version_id}.png"
        review.parent.mkdir(parents=True, exist_ok=True)
        strip.save(review, "PNG")
        summary = {"success": True, "version": result.version_id, "pin": result.pin, "method": result.method,
                   "summary": result.summary, "outside_changed_px": result.outside_changed_px,
                   "inside_changed_px": result.inside_changed_px, "review_image": str(review),
                   "next": "Compare before (left) and after (right) with the mark's note. Also check the mark's "
                           "border for a visible outline, halo, color shift or texture that does not match "
                           "(to look closer, vision_analyze review_image with a region). Retry with another "
                           "approach if anything falls short, then retouch_reply(status='needs_review')."}
        if result.spill:
            summary["edge_check"] = (
                f"The model also removed or changed something just OUTSIDE the mark at its {service.spill_text(result.spill)} "
                "edge: what you are removing (or its shadow) continues past the outline, and that part stays in "
                "the picture. A new attempt will not fix it. Say so in remaining_issues and ask the user to draw "
                "a bigger mark.")
        return _envelope(summary, [{"type": "text", "text": "Before (left) and after (right):"}, _image(strip)])
    return handle_edit


def handle_reply(args: dict, **_kw) -> str:
    if not isinstance(args.get("remaining_issues"), str):
        return _err(service.RetouchError("remaining_issues is required: what is still visibly wrong, or ''"))
    try:
        pin = service.Workspace.current().reply(str(args.get("project_id") or ""), int(args.get("pin") or 0),
                                                str(args.get("message") or ""), str(args.get("status") or ""),
                                                args.get("version"), args["remaining_issues"])
    except _ERRORS as exc:
        return _err(exc)
    return _ok(pin=_pin_brief(pin))


def handle_export(args: dict, **_kw) -> str:
    try:
        path = service.Workspace.current().export_and_keep(
            str(args.get("project_id") or ""), version_id=args.get("version"), fmt=args.get("format") or "png",
            quality=int(args.get("quality") or 90), max_side=args.get("max_side"),
            strip_gps=bool(args.get("strip_gps", True)), directory=args.get("directory"))
    except _ERRORS as exc:
        return _err(exc)
    return _ok(path=str(path), media=f"MEDIA:{path}")


def register(ctx) -> None:
    for schema, handler, emoji in ((GET, handle_get, "🖼️"), (EDIT, make_edit_handler(ctx), "🖌️"),
                                   (REPLY, handle_reply, "💬"), (EXPORT, handle_export, "💾")):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji)
