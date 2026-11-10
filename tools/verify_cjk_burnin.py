#!/usr/bin/env python3
"""Verify Korean burn-in subtitles render real glyphs (not tofu boxes).

Uses the project's real burn-in path: app.media.subtitle_render (ASS writer)
-> the same `subtitles=` filter graph as services/render -> ffmpeg_ops
.finalize_render, then extracts a frame (ffmpeg_ops.extract_frame) and OCRs it
with app.media.ocr. The burn-in check needs at least MIN_HANGUL of the cue's
Hangul characters in the OCR text.

Thumbnail check (review WA-M1): the real thumbnail path
(app.media.thumbnail_text.render_thumbnail) renders the short fixed title
THUMB_TITLE; OCR of its title band must contain every Hangul syllable of it.
Negative controls render the SAME title with the CJK font selection disabled
(1: Latin font forced, 2: Pillow's built-in default font) — tofu boxes. They
must NOT yield the title's Hangul; if they do, the OCR check cannot tell
glyphs from tofu and the script fails.

Exit codes: 0 all OK, 1 positive check failed (burn-in or thumbnail),
2 OCR engine unavailable, 3 a negative control unexpectedly recognized Hangul.

Run inside the image:  python /repo/tools/verify_cjk_burnin.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

# Config has no defaults for these; this tool never touches the DB.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("PROJECTS_ROOT", tempfile.gettempdir())

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

CUE_TEXT = "새벽이 온다. 등대가 불을 밝힌다."
MIN_HANGUL = 6
THUMB_TITLE = "등대"
WIDTH, HEIGHT, FPS = 1280, 720, 30


def _hangul(text: str) -> set[str]:
    return {c for c in text if "가" <= c <= "힣"}


def _thumbnail_hangul(force: str | None) -> tuple[set[str], str]:
    """Render THUMB_TITLE through the real thumbnail path and OCR its title
    band. `force`: None (production font selection), "latin" (CJK font
    selection disabled -> first Latin font) or "default" (Pillow default)."""
    import io

    from PIL import Image, ImageFont

    from app.media import ocr, thumbnail_text

    base = io.BytesIO()
    Image.new("RGB", (1280, 720), (40, 60, 90)).save(base, "PNG")
    with ExitStack() as stack:
        if force == "latin":
            stack.enter_context(mock.patch.object(thumbnail_text, "_needs_cjk", lambda t: False))
        elif force == "default":
            stack.enter_context(mock.patch.object(
                thumbnail_text, "_load_font",
                lambda size, cjk: ImageFont.load_default(size=size)))
        png = thumbnail_text.render_thumbnail(base.getvalue(), THUMB_TITLE)
    with tempfile.TemporaryDirectory(prefix="cjk-thumb-") as td:
        band = Path(td) / f"thumb_{force or 'cjk'}.png"
        img = Image.open(io.BytesIO(png))
        img.crop((0, thumbnail_text.THUMB_H - thumbnail_text._BAND_H,
                  thumbnail_text.THUMB_W, thumbnail_text.THUMB_H)).save(band)
        text = " ".join(h["text"] for h in ocr.recognize(str(band)))
    return _hangul(text), text


def check_thumbnail() -> int:
    expected = _hangul(THUMB_TITLE)
    found, text = _thumbnail_hangul(None)
    print(f"thumb title:  {THUMB_TITLE}")
    print(f"thumb ocr:    {text!r}")
    print(f"thumb hangul matched: {len(found & expected)}/{len(expected)} (need all)")
    if found & expected != expected:
        print("FAIL: Korean glyphs not recognized in the thumbnail title")
        return 1
    for force in ("latin", "default"):
        ctl_found, ctl_text = _thumbnail_hangul(force)
        hit = ctl_found & expected
        print(f"negative control ({force} font): ocr={ctl_text!r} "
              f"hangul matched {len(hit)}/{len(expected)} (need 0)")
        if hit:
            print(f"FAIL: negative control ({force}) recognized Hangul — the check "
                  "cannot tell glyphs from tofu")
            return 3
    print("thumbnail OK")
    return 0


def main() -> int:
    from app.media import ocr

    if not ocr.AVAILABLE:
        print("FAIL: OCR engine not available")
        return 2
    rc = check_burnin()
    rc_thumb = check_thumbnail()
    if rc or rc_thumb:
        return rc or rc_thumb
    print("OK")
    return 0


def check_burnin() -> int:
    from app.media import ffmpeg_ops, ocr, subtitle_render

    with tempfile.TemporaryDirectory(prefix="cjk-burnin-") as td:
        tmp = Path(td)
        src = tmp / "src.mp4"
        subprocess.run(
            [
                ffmpeg_ops.ffmpeg_bin(),
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d=2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(src),
            ],
            check=True,
        )

        cue = SimpleNamespace(
            start_sec=0.0, end_sec=2.0, text=CUE_TEXT, style_overrides=None
        )
        style = {
            "font_family": "Noto Sans KR",  # default style font (services/subtitles)
            "font_size_px": 64,
            "color": "#FFFFFF",
            "background_color": None,
            "position": "middle",
        }
        ass = subtitle_render.write_ass(
            [cue], style, tmp / "subs.ass", width=WIDTH, height=HEIGHT
        )

        esc = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        out = tmp / "out.mp4"
        ffmpeg_ops.finalize_render(
            [src],
            out,
            filter_complex=f"[0:v]subtitles='{esc}'[vout]",
            vmap="[vout]",
            amap="0:a",
            fps=FPS,
        )
        frame = tmp / "frame.png"
        ffmpeg_ops.extract_frame(out, frame, at_sec=1.0)

        hits = ocr.recognize(str(frame))
        text = " ".join(h["text"] for h in hits)

    expected = _hangul(CUE_TEXT)
    found = expected & _hangul(text)
    print(f"cue:  {CUE_TEXT}")
    print(f"ocr:  {text!r}")
    print(f"hangul matched: {len(found)}/{len(expected)} (need >= {MIN_HANGUL})")
    if len(found) >= MIN_HANGUL:
        print("burn-in OK")
        return 0
    print("FAIL: Korean glyphs not recognized in the burned-in frame")
    return 1


if __name__ == "__main__":
    sys.exit(main())
