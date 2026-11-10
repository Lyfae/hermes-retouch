"""Retouch's Hermes boundary: where projects live, the edit pipeline, and live updates.

Shared by the agent tools (``tools.py``) and the pane's REST routes (``dashboard/plugin_api.py``).
Hermes is imported lazily and only in ``Workspace.current()`` and ``notify()``, so tests drive a
``Workspace`` on a temporary folder.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import re
import shutil
import socket
import sqlite3
import ssl
import time
import urllib.parse
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


def _public_addresses(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, or an error when any of them is not a public one."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise RetouchError("the generated image's host could not be found") from None
    addresses = [str(info[4][0]) for info in found]
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise RetouchError("the image tool returned a link to a private address; Retouch will not fetch it")
    return addresses


def _require_public_https(url: str) -> None:
    """Refuse a link that is not https or whose host resolves to anything but a public address: the
    image provider's answer is untrusted, and the gateway can reach localhost and the home network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise RetouchError("the image tool returned a link Retouch will not fetch (https only)")
    _public_addresses(parts.hostname, parts.port or 443)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the very address that was checked, so a host cannot answer the check with a
    public address and the connection with a private one. Through a proxy the proxy resolves the
    name, and the check before the request is all there is."""

    def connect(self):
        if self._tunnel_host:
            return super().connect()
        address = _public_addresses(self.host, self.port)[0]
        sock = socket.create_connection((address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PublicHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=ssl.create_default_context())


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _inside(path: Path, roots) -> Path | None:
    """``path`` with symlinks resolved, when it is a file inside one of ``roots``; else None."""
    try:
        real = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file():
        return None
    for root in roots:
        try:
            if Path(root).resolve() in real.parents:
                return real
        except OSError:
            continue
    return None


def _decode(data: bytes, what: str) -> Image.Image:
    try:
        return ingest_mod.ingest(data).image
    except ingest_mod.IngestError as exc:
        raise RetouchError(f"{what} could not be used: {exc}") from None


def load_image_ref(ref: str, local_roots=()) -> Image.Image:
    """Open the image an image tool returned: a data: URL, an https URL on a public host, or a file
    inside ``local_roots`` (where Hermes saves generated images). Errors never repeat the reference,
    which can carry a signed token."""
    ref = str(ref or "").strip()
    if ref.startswith("data:"):
        _, sep, payload = ref.partition(",")
        if not sep or len(payload) > MAX_FETCH_BYTES * 4 // 3 + 4:
            raise RetouchError("the image tool returned no usable image")
        try:
            data = base64.b64decode(payload)
        except ValueError:
            raise RetouchError("the image tool returned no usable image") from None
    elif "://" in ref:
        _require_public_https(ref)
        opener = urllib.request.build_opener(_CheckedRedirects, _PublicHTTPS)
        try:
            with opener.open(urllib.request.Request(ref, headers={"User-Agent": "hermes-retouch"}), timeout=60) as resp:
                data = resp.read(MAX_FETCH_BYTES + 1)
        except RetouchError:
            raise
        except (OSError, ValueError):
            raise RetouchError("the generated image could not be downloaded") from None
    else:
        real = _inside(Path(ref), local_roots) if ref else None
        if real is None or real.stat().st_size > MAX_FETCH_BYTES:
            raise RetouchError("the image tool returned no usable image")
        data = real.read_bytes()
    if len(data) > MAX_FETCH_BYTES:
        raise RetouchError("the generated image is too large")
    return _decode(data, "the generated image")


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
        if not _PID.fullmatch(str(project_id)):
            raise RetouchError(f"{project_id!r} is not a Retouch project id")
        return self.root / "projects" / project_id

    def work_dir(self, project_id: str) -> Path:
        """Where the agent saves a full-size copy it edited itself; the only place from_file reads."""
        path = self.project_dir(project_id) / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

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
    def create_project(self, filename: str, data: bytes, *, session_id: str | None = None, scratch: bool = False) -> dict:
        try:
            got = ingest_mod.ingest(data)
        except ingest_mod.IngestError as exc:
            raise RetouchError(str(exc)) from None
        if scratch:
            self.sweep_scratch()
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
                                           size=got.image.size, first_version_file=first, project_id=project_id, scratch=scratch)
            if session_id:
                store.set_session(conn, project_id, session_id)
        return project

    # -- scratch projects: pasted images (screenshots) the user keeps or discards --------------------
    SCRATCH_TTL = 24 * 3600

    def keep(self, project_id: str) -> dict:
        with self.db() as conn:
            store.keep_project(conn, project_id)
        notify(project_id)
        return self.project(project_id)

    def discard(self, project_id: str) -> None:
        """Delete a scratch project. A kept project is refused: Discard exists for screenshots the
        user has not decided about, nothing else."""
        pdir = self.project_dir(project_id)
        with self.db() as conn:
            gone = store.delete_scratch_project(conn, project_id)
        if not gone:
            raise RetouchError("this project was kept; Retouch does not delete kept projects")
        shutil.rmtree(pdir, ignore_errors=True)
        notify(project_id)

    def sweep_scratch(self) -> list[str]:
        """Delete scratch projects the user never marked or edited and has not touched for
        SCRATCH_TTL; returns their ids. Each delete re-checks its own condition in the transaction."""
        with self.db() as conn:
            candidates = store.untouched_scratch_projects(conn, time.time() - self.SCRATCH_TTL)
        swept = []
        for pid in candidates:
            with self.db() as conn:
                gone = store.delete_scratch_project(conn, pid, only_untouched=True)
            if gone:
                shutil.rmtree(self.project_dir(pid), ignore_errors=True)
                swept.append(pid)
        return swept

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
            # Only a file inside this project's work folder: an agent steered by text in the picture
            # must not be able to pull some other image on the machine into the project.
            work = self.work_dir(project_id)
            real = _inside(Path(str(file or "")), [work])
            if real is None:
                raise RetouchError(f"file must be a full-size edited copy saved inside this project's work folder: {work}")
            if real.stat().st_size > ingest_mod.MAX_BYTES:
                raise RetouchError("that file is too large")
            src = _decode(real.read_bytes(), "that file")
            return (lambda base, mask, _report: self._blend(base, src, mask)), f"from {real.name}"
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

    def redo(self, project_id: str) -> dict:
        with self.db() as conn:
            proj = store.redo(conn, project_id)
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
               max_side: int | None = None, strip_gps: bool = True) -> Path:
        """Always into the project's own exports folder, as a new file."""
        proj = self.project(project_id)
        img, _ = self.load(project_id, version_id)
        try:
            data, ext = export_mod.encode(img, fmt, quality=quality, max_side=max_side, strip_gps=strip_gps,
                                          icc_profile=self._meta_bytes(project_id, "icc.bin"),
                                          exif=self._meta_bytes(project_id, "exif.bin"))
        except export_mod.ExportError as exc:
            raise RetouchError(str(exc)) from None
        return export_mod.write_new(self.project_dir(project_id) / "exports", f"{proj['title']}-retouched", data, ext)

    def export_and_keep(self, project_id: str, **kwargs) -> Path:
        """Saving a scratch project is the user keeping it."""
        path = self.export(project_id, **kwargs)
        if self.project(project_id)["scratch"]:
            self.keep(project_id)
        return path
