"""Retouch's Hermes boundary: where projects live, the edit pipeline, and live updates.

Shared by the agent tools (``tools.py``) and the pane's REST routes (``dashboard/plugin_api.py``).
Hermes is imported lazily and only in ``Workspace.current()`` and ``notify()``, so tests drive a
``Workspace`` on a temporary folder.
"""

from __future__ import annotations

import base64
import io
import re
import sqlite3
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image

from . import composite, geometry, ops as ops_mod, render, store
from . import export as export_mod
from . import ingest as ingest_mod

PLUGIN_ID = "retouch"
METHODS = ("adjust", "generate", "from_file")
MAX_FETCH_BYTES = 40 * 1024 * 1024
GENERATE_PROMPT = (
    "Edit this photo crop. The area to change is the middle of the crop; the edges are context. "
    "Change: {note}. Keep the framing, perspective, lighting, grain and colors identical to the "
    "surroundings, and leave everything outside the changed area exactly as it is."
)
_PID = re.compile(r"^rt_[0-9a-f]{12}$")

Generator = Callable[[Path, str], Image.Image]


class RetouchError(ValueError):
    """A problem the caller can fix; the message is shown to the user or the agent as-is."""


@dataclass
class EditResult:
    project_id: str
    pin: int
    version_id: str
    method: str
    summary: str
    outside_changed_px: int
    inside_changed_px: int
    before: Image.Image
    after: Image.Image
    spill: tuple[str, ...] = ()


def spill_text(sides) -> str:
    sides = list(sides)
    return " and ".join([", ".join(sides[:-1]), sides[-1]]) if len(sides) > 1 else "".join(sides)


def notify(project_id: str) -> None:
    """Tell every connected Desktop window that a project changed (no-op without a Desktop)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except ImportError:  # older Hermes: the pane's polling fallback picks the change up
        return
    broadcast_plugin_event(PLUGIN_ID, "project.updated", {"project_id": project_id})


def load_image_ref(ref: str) -> Image.Image:
    """Open an image an image tool returned: a data: URL, an http(s) URL or a local path."""
    ref = str(ref or "").strip()
    if ref.startswith("data:"):
        data = base64.b64decode(ref.split(",", 1)[1])
    elif ref.startswith(("https://", "http://")):
        with urllib.request.urlopen(urllib.request.Request(ref, headers={"User-Agent": "hermes-retouch"}), timeout=60) as resp:
            data = resp.read(MAX_FETCH_BYTES + 1)
        if len(data) > MAX_FETCH_BYTES:
            raise RetouchError("the generated image is too large")
    elif ref and Path(ref).expanduser().is_absolute() and Path(ref).expanduser().is_file():
        data = Path(ref).expanduser().read_bytes()
    else:
        raise RetouchError("the image tool returned no usable image")
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class Workspace:
    """One profile's Retouch data: ``<root>/data.db`` and ``<root>/projects/<id>/``."""

    def __init__(self, root: Path, connect: Callable[[], sqlite3.Connection] | None = None):
        self.root = Path(root)
        self._connect = connect or (lambda: sqlite3.connect(self.root / "data.db", timeout=10, check_same_thread=False))

    @classmethod
    def current(cls) -> "Workspace":
        """The active profile's workspace. Resolve per call: one process serves several profiles."""
        from plugins.plugin_storage import plugin_data_dir, plugin_db

        return cls(plugin_data_dir(PLUGIN_ID), lambda: plugin_db(PLUGIN_ID))

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = store.init(self._connect())
        try:
            yield conn
        finally:
            conn.close()

    # -- files ------------------------------------------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        if not _PID.match(str(project_id)):
            raise RetouchError(f"{project_id!r} is not a Retouch project id")
        return self.root / "projects" / project_id

    def _meta_bytes(self, project_id: str, name: str) -> bytes | None:
        path = self.project_dir(project_id) / name
        return path.read_bytes() if path.is_file() else None

    def _save_version_file(self, project_id: str, img: Image.Image) -> str:
        rel = f"versions/{store.new_id('f')}.png"
        path = self.project_dir(project_id) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        icc = self._meta_bytes(project_id, "icc.bin")
        img.save(path, "PNG", **({"icc_profile": icc} if icc else {}))
        return rel

    def load(self, project_id: str, version_id: str | None = None) -> tuple[Image.Image, dict]:
        with self.db() as conn:
            row = dict(store.version(conn, project_id, version_id))
        img = Image.open(self.project_dir(project_id) / row["file"])
        img.load()
        return img, row

    def version_path(self, project_id: str, version_id: str | None = None) -> Path:
        with self.db() as conn:
            return self.project_dir(project_id) / store.version(conn, project_id, version_id)["file"]

    # -- projects and marks -----------------------------------------------------------------------
    def create_project(self, filename: str, data: bytes, *, session_id: str | None = None) -> dict:
        try:
            got = ingest_mod.ingest(data)
        except ingest_mod.IngestError as exc:
            raise RetouchError(str(exc)) from None
        project_id = store.new_id("rt")
        pdir = self.project_dir(project_id)
        pdir.mkdir(parents=True)
        suffix = re.sub(r"[^a-z0-9.]", "", Path(filename or "").suffix.lower())[:8] or ".img"
        (pdir / f"original{suffix}").write_bytes(data)
        for name, blob in (("icc.bin", got.icc_profile), ("exif.bin", got.exif)):
            if blob:
                (pdir / name).write_bytes(blob)
        first = self._save_version_file(project_id, got.image)
        with self.db() as conn:
            project = store.create_project(conn, title=export_mod.safe_stem(filename), src_name=Path(filename or "image").name,
                                           size=got.image.size, first_version_file=first, project_id=project_id)
            if session_id:
                store.set_session(conn, project_id, session_id)
        return project

    def project(self, project_id: str) -> dict:
        self.project_dir(project_id)
        with self.db() as conn:
            return store.get_project(conn, project_id)

    def projects(self) -> list[dict]:
        with self.db() as conn:
            return store.list_projects(conn)

    def add_pin(self, project_id: str, *, shape: str, geom: dict, units: str = "px", intent: str = "fix",
                note: str = "") -> dict:
        proj = self.project(project_id)
        size = (proj["width"], proj["height"])
        try:
            norm = geometry.normalize(shape, geom, size) if units == "px" else geometry.validate(shape, geom)
            geometry.build_mask(shape, norm, size)
        except geometry.GeometryError as exc:
            raise RetouchError(str(exc)) from None
        with self.db() as conn:
            pin = store.add_pin(conn, project_id, shape=shape, geom=norm, intent=intent, note=note)
        notify(project_id)
        return pin

    def mask(self, pin: dict, size: tuple[int, int]) -> Image.Image:
        return geometry.build_mask(pin["shape"], pin["geom"], size)

    def update_pin(self, project_id: str, number: int, *, note: str | None = None, intent: str | None = None,
                   shape: str | None = None, geom: dict | None = None) -> dict:
        """Change a mark's note or intent, or move/redraw it (``geom`` in image pixels, as the pane
        draws it). Only an open mark can change shape: once sent, its edits belong to that outline."""
        norm = None
        if geom is not None:
            proj = self.project(project_id)
            current = next((p for p in proj["pins"] if p["number"] == int(number)), None)
            if current is None:
                raise RetouchError(f"there is no mark #{number}")
            if current["status"] != "open":
                raise RetouchError(f"mark #{number} was already sent; only open marks can be moved")
            shape = shape or current["shape"]
            size = (proj["width"], proj["height"])
            try:
                norm = geometry.normalize(shape, geom, size)
                geometry.build_mask(shape, norm, size)
            except geometry.GeometryError as exc:
                raise RetouchError(str(exc)) from None
        with self.db() as conn:
            pin = store.update_pin(conn, project_id, number, note=note, intent=intent,
                                   shape=shape if norm is not None else None, geom=norm)
        notify(project_id)
        return pin

    def delete_pin(self, project_id: str, number: int) -> None:
        with self.db() as conn:
            store.delete_pin(conn, project_id, number)
        notify(project_id)

    def send(self, project_id: str, numbers: list[int] | None = None) -> list[int]:
        """Queue the user's open marks for the agent; returns the mark numbers that were queued."""
        proj = self.project(project_id)
        chosen = [p["number"] for p in proj["pins"] if p["status"] == "open" and (not numbers or p["number"] in numbers)]
        if not chosen:
            raise RetouchError("no open marks to send")
        with self.db() as conn:
            for n in chosen:
                store.user_action(conn, project_id, n, "send")
        notify(project_id)
        return chosen

    # -- the edit pipeline ------------------------------------------------------------------------
    def edit(self, project_id: str, number: int, *, method: str, ops: list | None = None, prompt: str | None = None,
             file: str | None = None, generator: Generator | None = None) -> EditResult:
        if method not in METHODS:
            raise RetouchError(f"method must be one of {', '.join(METHODS)}")
        with self.db() as conn:
            pin = store.pin_dict(conn, store.require_pin(conn, project_id, number))
        note = (prompt or pin["note"] or "").strip()
        apply, summary = self._edit_fn(project_id, pin, method, ops, note, file, generator)
        with self.db() as conn:
            store.agent_set_status(conn, project_id, number, "working")
        notify(project_id)
        try:
            return self._apply_and_save(project_id, pin, number, method, apply, summary)
        except Exception:
            with self.db() as conn:
                store.restore_status(conn, project_id, number, pin["status"])
            notify(project_id)
            raise

    def _apply_and_save(self, project_id, pin, number, method, apply, summary) -> EditResult:
        for _attempt in range(3):
            base, base_row = self.load(project_id)
            mask = self.mask(pin, base.size)
            report: dict = {}
            after = apply(base, mask, report)
            spill = tuple(report.get("spill", ()))
            outside = composite.outside_changed_px(base, after, mask)
            inside = composite.inside_changed_px(base, after, mask)
            if inside == 0:
                raise RetouchError("the edit changed nothing inside the mark; try stronger values or another method")
            rel = self._save_version_file(project_id, after)
            try:
                with self.db() as conn:
                    vid = store.add_version(conn, project_id, file=rel, author="agent", pin=number, method=method,
                                            summary=summary, outside_changed_px=outside, inside_changed_px=inside,
                                            expected_parent=base_row["id"], spill=spill)
            except store.VersionConflict:
                (self.project_dir(project_id) / rel).unlink(missing_ok=True)
                continue
            except store.StoreError:
                (self.project_dir(project_id) / rel).unlink(missing_ok=True)
                raise
            view = composite.region_box(mask, 0.25)
            notify(project_id)
            return EditResult(project_id, number, vid, method, summary, outside, inside,
                              render.crop(base, view, mask), render.crop(after, view, mask), spill)
        raise RetouchError("the image kept changing while editing; try again")

    def _edit_fn(self, project_id, pin, method, ops, note, file, generator):
        if method == "adjust":
            try:
                clean = ops_mod.normalize_ops(ops or [])
            except ops_mod.OpError as exc:
                raise RetouchError(str(exc)) from None
            summary = ", ".join(f"{n} {v:+g}" if isinstance(v, float) else f"{n} {v}" for n, v in clean)
            edge = ops_mod.edge_for(clean)

            def adjust(base, mask, _report):
                feather = {"soft": composite.soft_feather(mask), "hard": 0}.get(edge)
                return composite.apply_in_region(base, mask, lambda c, s: ops_mod.run_ops(c, clean), feather=feather)

            return adjust, summary
        if method == "from_file":
            path = Path(str(file or "")).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise RetouchError("file must be an absolute path to a full-size edited copy of the image")
            src = Image.open(path)
            src.load()
            return (lambda base, mask, _report: self._blend(base, src, mask)), f"from {path.name}"
        if generator is None:
            raise RetouchError("generative edits need an image generation provider (set one up in `hermes tools`)")
        if not note:
            raise RetouchError("describe the change: the mark has no note and no prompt was given")
        text = GENERATE_PROMPT.format(note=note)
        generated: list[Image.Image] = []

        def gen(crop: Image.Image, _soft: Image.Image) -> Image.Image:
            if not generated:  # a retry after a version conflict reuses the (paid) result
                path = self.project_dir(project_id) / "crops" / f"pin{pin['number']}-r{pin['round']}-{store.new_id('c')}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                crop.save(path, "PNG")
                result = generator(path, text)
                result.save(path.with_name(f"{path.stem}-out.png"), "PNG")  # what the model returned, for review
                generated.append(result)
            return generated[0]

        def generate(base, mask, report):
            return composite.apply_in_region(base, mask, gen, color_match=True, square=True,
                                             feather=composite.generate_feather(base.size), report=report)

        return generate, f"generated: {note[:120]}"

    @staticmethod
    def _blend(base: Image.Image, src: Image.Image, mask: Image.Image) -> Image.Image:
        try:
            return composite.blend_full(base, src, mask)
        except ValueError as exc:
            raise RetouchError(str(exc)) from None

    # -- conversation and review ------------------------------------------------------------------
    def reply(self, project_id: str, number: int, message: str, status: str, version_id: str | None = None,
              issues: str = "") -> dict:
        with self.db() as conn:
            spill = store.result_spill(conn, project_id, number)
            if spill and status == "needs_review" and not issues.strip():
                raise RetouchError(f"the edge check found the object continuing past the mark at its {spill_text(spill)} "
                                   "edge; say what remains there in remaining_issues")
            store.agent_set_status(conn, project_id, number, status)
            store.add_message(conn, project_id, number, "agent", message, version_id, issues)
            pin = store.pin_dict(conn, store.require_pin(conn, project_id, number))
        notify(project_id)
        return pin

    def user_action(self, project_id: str, number: int, action: str, message: str = "") -> dict:
        with self.db() as conn:
            pin = store.user_action(conn, project_id, number, action, message)
        notify(project_id)
        return pin

    def user_message(self, project_id: str, number: int, message: str) -> dict:
        with self.db() as conn:
            store.add_message(conn, project_id, number, "user", message)
            pin = store.pin_dict(conn, store.require_pin(conn, project_id, number))
        notify(project_id)
        return pin

    def revert(self, project_id: str, *, to_version: str | None = None, pin: int | None = None) -> dict:
        with self.db() as conn:
            proj = store.revert(conn, project_id, to_version=to_version, pin=pin)
        notify(project_id)
        return proj

    # -- what the agent and the pane look at -------------------------------------------------------
    def views(self, project_id: str, numbers: list[int] | None = None, max_crops: int = 6) -> tuple[dict, Image.Image, list]:
        proj = self.project(project_id)
        img, _ = self.load(project_id)
        marks = [(p["number"], self.mask(p, img.size)) for p in proj["pins"]]
        wanted = set(numbers or [p["number"] for p in proj["pins"] if p["status"] not in store.CLOSED])
        crops = [(n, render.crop(img, composite.region_box(m, 0.25), m)) for n, m in marks if n in wanted][:max_crops]
        return proj, render.overview(img, marks), crops

    def export(self, project_id: str, *, version_id: str | None = None, fmt: str = "png", quality: int = 90,
               max_side: int | None = None, strip_gps: bool = True, directory: str | None = None) -> Path:
        proj = self.project(project_id)
        img, _ = self.load(project_id, version_id)
        try:
            data, ext = export_mod.encode(img, fmt, quality=quality, max_side=max_side, strip_gps=strip_gps,
                                          icc_profile=self._meta_bytes(project_id, "icc.bin"),
                                          exif=self._meta_bytes(project_id, "exif.bin"))
        except export_mod.ExportError as exc:
            raise RetouchError(str(exc)) from None
        dest = Path(directory).expanduser() if directory else self.project_dir(project_id) / "exports"
        if not dest.is_absolute():
            raise RetouchError("directory must be an absolute path")
        return export_mod.write_new(dest, f"{proj['title']}-retouched", data, ext)
