"""Provider result validation: byte caps, Content-Type and magic bytes
(ADR-0014 §4, audit F16/P16).

A provider "200 OK" is not proof of media: an HTML error page, a JSON error
body or a truncated download must never be saved as an artifact. Pure
functions; the http adapter applies them while streaming the download.
Images must also DECODE (B7-r2 M3): a PNG signature followed by garbage is
not an image. ``check_image_decodes`` is applied by the http adapter and by
the image worker before any result is published (every adapter).
"""

from __future__ import annotations

import io

from PIL import Image

from app.core import defaults
from app.core.errors import ErrorCode, StudioError

# Content types that are never a media artifact.
_TEXT_TYPES = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/problem+json",
)
# Generic binary types accepted for any media kind (magic bytes decide).
_GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream", "")


def max_bytes(modality: str) -> int | None:
    """Download cap for a media kind (None = no media cap, e.g. text)."""
    return {
        "image": defaults.HTTP_RESULT_MAX_BYTES_IMAGE,
        "video": defaults.HTTP_RESULT_MAX_BYTES_VIDEO,
        "audio": defaults.HTTP_RESULT_MAX_BYTES_AUDIO,
        "tts": defaults.HTTP_RESULT_MAX_BYTES_AUDIO,
    }.get(modality)


def _family(modality: str) -> str | None:
    return {"image": "image", "video": "video", "audio": "audio", "tts": "audio"}.get(
        modality
    )


def sniff(data: bytes) -> str | None:
    """Media kind from magic bytes: 'image' | 'video' | 'audio' | None."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio"
    if head.startswith((b"OggS", b"fLaC", b"ID3")):
        return "audio"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio"  # MPEG audio frame sync
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "audio" if brand in (b"M4A ", b"M4B ") else "video"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"  # Matroska / WebM
    return None


def check_content_type(content_type: str | None, modality: str, *, what: str) -> None:
    """Reject a Content-Type that cannot be this media kind (before reading)."""
    family = _family(modality)
    if family is None:
        return
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _GENERIC_TYPES:
        return
    major = ctype.split("/", 1)[0]
    same_family = major == family or (family == "video" and ctype == "application/mp4")
    if major == "text" or ctype in _TEXT_TYPES or not same_family:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: provider returned Content-Type {ctype!r}, not {family} media.",
            details={"reason": "result_content_type", "content_type": ctype},
        )


def check_size(size: int, modality: str, *, what: str) -> None:
    cap = max_bytes(modality)
    if cap is not None and size > cap:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result exceeds the {modality} size cap ({size} > {cap} bytes).",
            details={"reason": "result_too_large", "max_bytes": cap},
        )


def check_media(data: bytes, modality: str, *, what: str) -> None:
    """Magic-byte check of the complete artifact."""
    family = _family(modality)
    if family is None:
        return
    check_size(len(data), modality, what=what)
    if sniff(data) != family:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result bytes are not a recognized {family} format.",
            details={"reason": "result_not_media", "expected": family},
        )
    if family == "image":
        check_image_decodes(data, what=what)


def check_image_decodes(data: bytes, *, what: str) -> None:
    """The bytes are ONE decodable still image, as far as Pillow verify +
    load can tell (container trailers that Pillow tolerates, e.g. a GIF
    missing its final trailer byte, still pass — review B7R3-L1).

    Multi-frame images (animated GIF/PNG/WebP) are rejected (B7-r3 M3): the
    pipeline publishes still frames (a frame take, a character sheet image,
    a thumbnail), so an animation would be silently flattened to its first
    frame — and ``load()`` decodes only that frame, so a broken later frame
    would pass. Rejecting is simpler and stricter than decoding every frame
    under frame/pixel caps. The pixel count is capped too (decompression
    bomb), before any decode.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            width, height = im.size
    except Exception as exc:  # noqa: BLE001 - any decoder failure = not an image
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result image cannot be decoded "
            f"({exc.__class__.__name__}: {str(exc)[:120]}).",
            details={"reason": "result_not_decodable"},
        ) from exc
    if frames != 1:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result is a {frames}-frame image; only still images "
            "(one frame) are accepted.",
            details={"reason": "result_multi_frame", "frames": frames},
        )
    max_pixels = Image.MAX_IMAGE_PIXELS
    if max_pixels and width * height > max_pixels:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result image is {width}x{height}, over the "
            f"{max_pixels}-pixel decode cap.",
            details={"reason": "result_too_large", "max_pixels": max_pixels},
        )
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # structure + checksums (PNG CRCs)
        with Image.open(io.BytesIO(data)) as im:
            im.load()  # full pixel decode (truncation)
    except Exception as exc:  # noqa: BLE001 - any decoder failure = not an image
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: result image cannot be decoded "
            f"({exc.__class__.__name__}: {str(exc)[:120]}).",
            details={"reason": "result_not_decodable"},
        ) from exc
