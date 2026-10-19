"""Thumbnail generation (worker-only; never called from API routers).

Phase-1 scope: still-image thumbnails (JPG, quality 85 per [가정] #13).
Video/contact-sheet thumbnails arrive with ffmpeg in Phase 3.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.core import defaults
from app.core.errors import ErrorCode, StudioError

THUMB_SIZE = (320, 320)


def make_image_thumbnail(src: Path, dst: Path, size: tuple[int, int] = THUMB_SIZE) -> Path:
    """Create a JPG thumbnail of an image file. Returns dst.

    Raises StudioError(VALIDATION_ERROR) when src is not a readable image.
    """
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail(size, Image.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst, "JPEG", quality=defaults.THUMBNAIL_JPEG_QUALITY)
    except StudioError:
        raise
    except Exception as exc:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Cannot create thumbnail for {src.name}.",
            details={"reason": str(exc)},
        )
    return dst


def thumbnail_path_for(image_path: Path) -> Path:
    """Sibling thumbnail path: <stem>.thumb.jpg next to the image."""
    return image_path.with_name(f"{image_path.stem}.thumb.jpg")
