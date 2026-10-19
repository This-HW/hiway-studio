"""Thumbnail post-processing: title text overlay (P1-9, design §10.5).

Text is NEVER rendered by the image model — it is composited here with PIL
after the image bytes are generated. Canvas is 1280x720 (16:9); the generated
image is cover-fit onto the canvas, then an optional bottom band with the
centered title text is drawn.

Font notes: DejaVu Sans Bold for Latin text; Noto Sans CJK for CJK text
(Korean titles are the common case). Both ship with the system fonts here;
when neither loads, Pillow's built-in bitmap font is the last resort.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from app.core.errors import ErrorCode, StudioError

THUMB_W = 1280
THUMB_H = 720
_BAND_H = 190

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_NOTO_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def _needs_cjk(text: str) -> bool:
    return any(ord(c) > 0x2FFF for c in text)


def _load_font(size: int, cjk: bool):
    """Best-effort font loading; never raises."""
    candidates: list[tuple[str, dict]] = []
    if cjk:
        # .ttc collection: try the first few faces (JP/HK/KR/CN/TW order varies).
        candidates += [(_NOTO_CJK, {"index": i}) for i in range(5)]
    candidates.append((_DEJAVU_BOLD, {}))
    for path, kw in candidates:
        try:
            return ImageFont.truetype(path, size=size, **kw)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _fit_font(text: str, max_width: int, start_size: int = 84) -> object:
    cjk = _needs_cjk(text)
    size = start_size
    probe = Image.new("RGB", (8, 8))
    draw = ImageDraw.Draw(probe)
    while size > 12:
        font = _load_font(size, cjk)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 6
    return _load_font(12, cjk)


def render_thumbnail(data: bytes, title_text: str | None) -> bytes:
    """Composite the generated image onto a 1280x720 canvas and overlay the
    title text band. Returns PNG bytes.

    Raises StudioError(VALIDATION_ERROR) when the input is not a readable image.
    """
    try:
        src = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Cannot decode generated image for thumbnail.",
            details={"reason": str(exc)},
        )

    # Cover-fit onto the canvas.
    scale = max(THUMB_W / src.width, THUMB_H / src.height)
    fitted = src.resize(
        (round(src.width * scale), round(src.height * scale)), Image.LANCZOS
    )
    left = (fitted.width - THUMB_W) // 2
    top = (fitted.height - THUMB_H) // 2
    canvas = fitted.crop((left, top, left + THUMB_W, top + THUMB_H))

    if title_text and title_text.strip():
        text = title_text.strip()
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle(
            [0, THUMB_H - _BAND_H, THUMB_W, THUMB_H], fill=(0, 0, 0, 160)
        )
        font = _fit_font(text, THUMB_W - 160)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (THUMB_W - tw) // 2 - bbox[0]
        ty = (THUMB_H - _BAND_H + (_BAND_H - th) // 2) - bbox[1]
        # Soft shadow for legibility.
        draw.text((tx + 3, ty + 3), text, font=font, fill=(0, 0, 0, 220))
        draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()
