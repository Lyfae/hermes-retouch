"""Mark geometry: normalized shapes in, full-resolution hard masks out.

Geometry is stored normalized to 0..1 of the image size so a mark keeps its place when the image
is shown at a different scale. Brush width is normalized to the image's shorter side.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageDraw

SHAPES = ("ellipse", "rect", "lasso", "brush")


class GeometryError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GeometryError(f"{name} must be a number") from None
    if not math.isfinite(number):
        raise GeometryError(f"{name} must be finite")
    return number


def _points(geom: dict, minimum: int) -> list[tuple[float, float]]:
    raw = geom.get("points")
    if not isinstance(raw, list) or len(raw) < minimum:
        raise GeometryError(f"points needs at least {minimum} [x, y] pairs")
    points = []
    for i, pair in enumerate(raw):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise GeometryError(f"points[{i}] must be [x, y]")
        points.append((_finite(pair[0], f"points[{i}].x"), _finite(pair[1], f"points[{i}].y")))
    return points


def validate(shape: str, geom: dict) -> dict:
    """Return a clean normalized copy of ``geom`` for ``shape`` or raise GeometryError."""
    if shape not in SHAPES:
        raise GeometryError(f"shape must be one of {', '.join(SHAPES)}")
    if not isinstance(geom, dict):
        raise GeometryError("geometry must be an object")
    if shape in ("ellipse", "rect"):
        x, y = _finite(geom.get("x"), "x"), _finite(geom.get("y"), "y")
        w, h = _finite(geom.get("w"), "w"), _finite(geom.get("h"), "h")
        if w <= 0 or h <= 0:
            raise GeometryError("w and h must be positive")
        return {"x": x, "y": y, "w": w, "h": h}
    if shape == "lasso":
        return {"points": [list(p) for p in _points(geom, 3)]}
    width = _finite(geom.get("width"), "width")
    if width <= 0:
        raise GeometryError("width must be positive")
    return {"points": [list(p) for p in _points(geom, 1)], "width": width}


def normalize(shape: str, geom_px: dict, size: tuple[int, int]) -> dict:
    """Pixel geometry (as the pane draws it) → normalized geometry."""
    w, h = size
    if shape in ("ellipse", "rect"):
        return validate(shape, {"x": geom_px.get("x", 0) / w, "y": geom_px.get("y", 0) / h,
                                "w": geom_px.get("w", 0) / w, "h": geom_px.get("h", 0) / h})
    pts = [[p[0] / w, p[1] / h] for p in geom_px.get("points") or [] if isinstance(p, (list, tuple)) and len(p) == 2]
    out = {"points": pts}
    if shape == "brush":
        out["width"] = (geom_px.get("width") or 0) / min(w, h)
    return validate(shape, out)


def build_mask(shape: str, geom: dict, size: tuple[int, int]) -> Image.Image:
    """Binary ``L`` mask (0 or 255) at full resolution. Raises GeometryError if the mark covers no pixels."""
    geom = validate(shape, geom)
    w, h = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    if shape in ("ellipse", "rect"):
        box = [geom["x"] * w, geom["y"] * h, (geom["x"] + geom["w"]) * w, (geom["y"] + geom["h"]) * h]
        (draw.ellipse if shape == "ellipse" else draw.rectangle)(box, fill=255)
    elif shape == "lasso":
        draw.polygon([(x * w, y * h) for x, y in geom["points"]], fill=255)
    else:
        stroke = max(1.0, geom["width"] * min(w, h))
        pts = [(x * w, y * h) for x, y in geom["points"]]
        if len(pts) > 1:
            draw.line(pts, fill=255, width=max(1, round(stroke)), joint="curve")
        r = stroke / 2
        for x, y in pts:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=255)
    mask = mask.point(lambda v: 255 if v >= 128 else 0)
    if mask.getbbox() is None:
        raise GeometryError("the mark does not cover any pixels of the image")
    return mask
