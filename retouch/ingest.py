"""Open user images safely: EXIF orientation applied, alpha kept, ICC profile and EXIF remembered."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 60_000_000
MAX_BYTES = 64 * 1024 * 1024

try:  # optional: HEIC/HEIF photos when pillow-heif is present (Hermes ships it on Python >= 3.14)
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = True
except Exception:  # pragma: no cover - depends on the host
    HEIF = False


class IngestError(ValueError):
    pass


@dataclass
class Ingested:
    image: Image.Image
    icc_profile: bytes | None = None
    exif: bytes | None = None
    source_format: str = ""
    meta: dict = field(default_factory=dict)


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def ingest(data: bytes) -> Ingested:
    if not data:
        raise IngestError("the file is empty")
    if len(data) > MAX_BYTES:
        raise IngestError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise IngestError(f"the image is larger than {MAX_PIXELS // 1_000_000} megapixels")
        img.load()
    except IngestError:
        raise
    except Image.DecompressionBombError:  # Pillow refuses huge images at open, before the check above
        raise IngestError(f"the image is larger than {MAX_PIXELS // 1_000_000} megapixels") from None
    except UnidentifiedImageError:
        formats = "PNG, JPEG, WebP, TIFF or HEIC" if HEIF else "PNG, JPEG, WebP or TIFF (HEIC photos need pillow-heif)"
        raise IngestError(f"this file is not an image Retouch can open; use {formats}") from None
    except Exception:
        raise IngestError("the image file is damaged or incomplete and could not be read") from None
    fmt = img.format or ""
    icc = img.info.get("icc_profile")
    exif = img.info.get("exif")
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    return Ingested(image=img, icc_profile=icc, exif=exif, source_format=fmt,
                    meta={"width": img.width, "height": img.height, "alpha": img.mode == "RGBA"})
