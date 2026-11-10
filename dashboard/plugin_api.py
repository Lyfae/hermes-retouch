"""Retouch pane backend, mounted at /api/plugins/retouch/. Every call here is the USER acting; the
agent acts only through the tools. Routes are sync on purpose: Hermes runs them in a worker thread
with the request's profile scope copied in, so ``Workspace.current()`` resolves the right profile."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

_LIB = "hermes_retouch_lib"


def _service():
    """The plugin's own library, loaded by path under a private name. This file is imported on its
    own (not as part of the plugin package), so a relative import cannot reach ``retouch/``."""
    if _LIB not in sys.modules:
        root = Path(__file__).resolve().parents[1] / "retouch"
        spec = importlib.util.spec_from_file_location(_LIB, root / "__init__.py", submodule_search_locations=[str(root)])
        assert spec is not None and spec.loader is not None  # a .py path always yields a loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_LIB] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{_LIB}.service")


svc = _service()
_store = importlib.import_module(f"{_LIB}.store")
_geometry = importlib.import_module(f"{_LIB}.geometry")
_render = importlib.import_module(f"{_LIB}.render")
_USER_ERRORS = (svc.RetouchError, _store.StoreError, _geometry.GeometryError)

router = APIRouter()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except _USER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _ws():
    return svc.Workspace.current()


class PinIn(BaseModel):
    shape: str
    geom: dict
    intent: str = "fix"
    note: str = ""


class PinPatch(BaseModel):
    note: Optional[str] = None
    intent: Optional[str] = None
    shape: Optional[str] = None
    geom: Optional[dict] = None


class MessageIn(BaseModel):
    body: str


class ActionIn(BaseModel):
    action: str
    message: str = ""


class SendIn(BaseModel):
    pins: Optional[list[int]] = None


class RevertIn(BaseModel):
    version: Optional[str] = None
    pin: Optional[int] = None


class ExportIn(BaseModel):
    version: Optional[str] = None
    format: str = "png"
    quality: int = 90
    max_side: Optional[int] = None
    strip_gps: bool = True


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    return {"projects": _ws().projects()}


@router.post("/projects")
def create_project(file: UploadFile = File(...), session_id: Optional[str] = None) -> dict[str, Any]:
    data = file.file.read(svc.ingest_mod.MAX_BYTES + 1)
    return _call(_ws().create_project, file.filename or "image", data, session_id=session_id)


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return _call(_ws().project, project_id)


@router.get("/projects/{project_id}/image")
def get_image(project_id: str, version: Optional[str] = None, max_px: int = 2048) -> dict[str, Any]:
    """The image for display, downscaled to ``max_px``; ``scale`` maps display pixels back to the original."""
    img, row = _call(_ws().load, project_id, version)
    width, height = img.size
    if max_px and max(width, height) > max_px:
        img = img.copy()
        img.thumbnail((max_px, max_px))
    fmt = "PNG" if img.mode == "RGBA" else "JPEG"
    return {"version": row["id"], "width": width, "height": height, "scale": img.width / width,
            "data_url": _render.data_url(img, fmt, 90)}


@router.post("/projects/{project_id}/pins")
def add_pin(project_id: str, body: PinIn) -> dict[str, Any]:
    return _call(_ws().add_pin, project_id, shape=body.shape, geom=body.geom, units="px", intent=body.intent,
                 note=body.note)


@router.patch("/projects/{project_id}/pins/{number}")
def update_pin(project_id: str, number: int, body: PinPatch) -> dict[str, Any]:
    return _call(_ws().update_pin, project_id, number, note=body.note, intent=body.intent, shape=body.shape,
                 geom=body.geom)


@router.delete("/projects/{project_id}/pins/{number}")
def delete_pin(project_id: str, number: int) -> dict[str, Any]:
    _call(_ws().delete_pin, project_id, number)
    return {"ok": True}


@router.post("/projects/{project_id}/pins/{number}/messages")
def add_message(project_id: str, number: int, body: MessageIn) -> dict[str, Any]:
    return _call(_ws().user_message, project_id, number, body.body)


@router.post("/projects/{project_id}/pins/{number}/action")
def pin_action(project_id: str, number: int, body: ActionIn) -> dict[str, Any]:
    return _call(_ws().user_action, project_id, number, body.action, body.message)


@router.post("/projects/{project_id}/send")
def send(project_id: str, body: SendIn) -> dict[str, Any]:
    queued = _call(_ws().send, project_id, body.pins)
    marks = ", ".join(f"#{n}" for n in queued)
    return {"queued": queued,
            "prompt": f"Retouch project {project_id}: please work on mark{'s' if len(queued) > 1 else ''} {marks}. "
                      f"Start with retouch_get."}


@router.post("/projects/{project_id}/revert")
def revert(project_id: str, body: RevertIn) -> dict[str, Any]:
    return _call(_ws().revert, project_id, to_version=body.version, pin=body.pin)


@router.post("/projects/{project_id}/redo")
def redo(project_id: str) -> dict[str, Any]:
    return _call(_ws().redo, project_id)


@router.post("/projects/{project_id}/export")
def export(project_id: str, body: ExportIn) -> dict[str, Any]:
    path = _call(_ws().export_and_keep, project_id, version_id=body.version, fmt=body.format, quality=body.quality,
                 max_side=body.max_side, strip_gps=body.strip_gps)
    return {"path": str(path), "name": path.name}


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {"screenshot": _ws().can_screenshot()}


class ScreenshotIn(BaseModel):
    session_id: Optional[str] = None


@router.post("/screenshot")
def screenshot(body: ScreenshotIn) -> dict[str, Any]:
    """Opens the OS region picker on the gateway host; the capture becomes a scratch project."""
    project = _call(_ws().screenshot, session_id=body.session_id)
    return {"project": project}  # null when the user cancelled the picker


@router.post("/projects/{project_id}/keep")
def keep(project_id: str) -> dict[str, Any]:
    return _call(_ws().keep, project_id)


@router.delete("/projects/{project_id}")
def discard(project_id: str) -> dict[str, Any]:
    _call(_ws().discard, project_id)
    return {"ok": True}
