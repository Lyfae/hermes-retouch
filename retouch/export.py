"""Write finished images as NEW files: never overwrite, strip GPS by default, keep the color profile."""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import ExifTags, Image

FORMATS = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}
_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


class ExportError(ValueError):
    pass


def clean_exif(exif_bytes: bytes | None, strip_gps: bool) -> bytes | None:
    if not exif_bytes:
        return None
    exif = Image.Exif()
    try:
        exif.load(exif_bytes)
    except Exception:
        return None
    exif.pop(ExifTags.Base.Orientation, None)  # pixels are already upright
    if strip_gps:
        exif.pop(ExifTags.IFD.GPSInfo, None)
    return exif.tobytes()


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name or "image").stem).strip(".-") or "image"
    return stem[:80]


def encode(img: Image.Image, fmt: str = "png", *, quality: int = 90, max_side: int | None = None,
           strip_gps: bool = True, icc_profile: bytes | None = None, exif: bytes | None = None) -> tuple[bytes, str]:
    """Return (encoded bytes, file extension)."""
    pil_fmt = FORMATS.get(str(fmt).lower())
    if pil_fmt is None:
        raise ExportError(f"format must be one of {', '.join(sorted(set(FORMATS) - {'jpg'}))}")
    out = img
    if max_side and max(out.size) > int(max_side):
        out = out.copy()
        out.thumbnail((int(max_side), int(max_side)), Image.Resampling.LANCZOS)
    if pil_fmt == "JPEG" and out.mode == "RGBA":
        flat = Image.new("RGB", out.size, (255, 255, 255))
        flat.paste(out, mask=out.getchannel("A"))
        out = flat
    kwargs: dict = {}
    if icc_profile:
        kwargs["icc_profile"] = icc_profile
    cleaned = clean_exif(exif, strip_gps)
    if cleaned:
        kwargs["exif"] = cleaned
    if pil_fmt in ("JPEG", "WEBP"):
        kwargs["quality"] = max(1, min(100, int(quality)))
    buf = io.BytesIO()
    out.save(buf, pil_fmt, **kwargs)
    return buf.getvalue(), _EXT[pil_fmt]


def write_new(dest_dir: Path, stem: str, data: bytes, ext: str) -> Path:
    """Create a file that did not exist before (``stem.ext``, then ``stem-2.ext``, ...)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        path = dest_dir / (f"{stem}{ext}" if i == 1 else f"{stem}-{i}{ext}")
        try:
            with open(path, "xb") as fh:
                fh.write(data)
            return path
        except FileExistsError:
            continue
    raise ExportError("could not find a free file name")
