"""Images the agent and the pane look at: numbered overview, per-mark crops, before/after strips."""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw, ImageFont

MARK = (255, 45, 149)  # high-visibility magenta, readable on most photos
HALO = (255, 255, 255)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def flatten(img: Image.Image, bg=(128, 128, 128)) -> Image.Image:
    if img.mode != "RGBA":
        return img.convert("RGB")
    out = Image.new("RGB", img.size, bg)
    out.paste(img, mask=img.getchannel("A"))
    return out


def outline(mask: Image.Image, width: int) -> Image.Image:
    from PIL import ImageChops, ImageFilter

    k = max(3, width * 2 + 1) | 1
    return ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(k)), mask)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, scale: float) -> None:
    r = max(10, round(14 * scale))
    x, y = xy
    draw.ellipse([x - r - 2, y - r - 2, x + r + 2, y + r + 2], fill=HALO)
    draw.ellipse([x - r, y - r, x + r, y + r], fill=MARK)
    draw.text((x, y), text, fill=(255, 255, 255), font=_font(round(r * 1.3)), anchor="mm")


def overview(img: Image.Image, marks: list[tuple[int, Image.Image]], max_side: int = 1568) -> Image.Image:
    """The whole image scaled down with every mark outlined and numbered."""
    base = flatten(img)
    scale = min(1.0, max_side / max(base.size))
    size = (max(1, round(base.width * scale)), max(1, round(base.height * scale)))
    out = base.resize(size, Image.Resampling.LANCZOS)
    s = max(out.size) / 1000
    for number, mask in marks:
        small = mask.resize(size, Image.Resampling.NEAREST)
        ring = outline(small, max(2, round(3 * s)))
        out.paste(Image.new("RGB", size, MARK), mask=ring)
        box = small.getbbox()
        if box:
            _label(ImageDraw.Draw(out), (box[0], box[1]), str(number), s)
    return out


def crop(img: Image.Image, box: tuple[int, int, int, int], mask: Image.Image | None = None,
         max_side: int = 768) -> Image.Image:
    out = flatten(img.crop(box))
    if mask is not None:
        ring = outline(mask.crop(box), max(1, round(max(out.size) / 300)))
        out.paste(Image.new("RGB", out.size, MARK), mask=ring)
    if max(out.size) > max_side:
        out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return out


def before_after(before: Image.Image, after: Image.Image, gap: int = 8) -> Image.Image:
    h = max(before.height, after.height)
    out = Image.new("RGB", (before.width + after.width + gap, h), (255, 255, 255))
    out.paste(before, (0, 0))
    out.paste(after, (before.width + gap, 0))
    return out


def data_url(img: Image.Image, fmt: str = "JPEG", quality: int = 88) -> str:
    buf = io.BytesIO()
    rgb = flatten(img) if fmt == "JPEG" else img
    rgb.save(buf, fmt, **({"quality": quality} if fmt == "JPEG" else {}))
    mime = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
