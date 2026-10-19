"""ffmpeg operations (design report §2.2.6).

Normalization rules: output follows the timeline's export preset; defaults are
1920x1080 · 24fps · H.264 · AAC 48kHz ([assumption] #4). Aspect-ratio mismatches
are letterboxed (black padding), fps mismatches use nearest (no interpolation),
audio is resampled to 48kHz.

This module owns the ffmpeg subprocess boundary only. It never imports
services (ARCHITECTURE §15): callers in services/ and tasks/ compose these
primitives. The ffmpeg binary is resolved from FFMPEG_BIN (default "ffmpeg").
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.core.errors import ErrorCode, StudioError

log = logging.getLogger("ai_studio.media.ffmpeg")


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def ffprobe_bin() -> str:
    """ffprobe binary; follows FFMPEG_BIN's sibling when a custom ffmpeg is set."""
    custom = os.environ.get("FFMPEG_BIN")
    if custom:
        sibling = Path(custom).with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    return "ffprobe"


class FFmpegError(StudioError):
    """Local encode failure. Not retryable: encode errors are deterministic."""

    def __init__(self, code: str, message: str, **kwargs) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(code, message, **kwargs)


def _run(args: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_bin(), *args],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffmpeg binary not found (FFMPEG_BIN={ffmpeg_bin()!r}).",
            details={"what": what},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffmpeg timed out: {what}.",
            details={"what": what},
        ) from exc
    if proc.returncode != 0:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffmpeg failed: {what}.",
            details={"what": what, "stderr_tail": proc.stderr[-2000:]},
        )
    return proc


def probe(path: str | Path) -> dict:
    """Return media facts via ffprobe JSON. Raises FFmpegError on unreadable file.

    (`ffmpeg -i` with no output file exits non-zero, so stderr parsing is not
    a reliable probe; ffprobe is part of every standard ffmpeg install.)
    """
    import json as _json

    try:
        proc = subprocess.run(
            [
                ffprobe_bin(),
                "-v", "error",
                # No -select_streams: callers need both video AND audio streams
                # (has_audio would otherwise always be false).
                "-show_entries", "stream=index,codec_type,codec_name,width,height,avg_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffprobe binary not found (FFMPEG_BIN={ffmpeg_bin()!r}).",
            details={"what": f"probe {path}"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffprobe timed out: probe {path}.",
            details={"what": f"probe {path}"},
        ) from exc
    if proc.returncode != 0:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffprobe failed: probe {path}.",
            details={"what": f"probe {path}", "stderr_tail": proc.stderr[-2000:]},
        )
    try:
        info = _json.loads(proc.stdout)
    except ValueError as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffprobe returned invalid JSON: probe {path}.",
            details={"what": f"probe {path}"},
        ) from exc
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), streams[0] if streams else {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fps = 0.0
    num, _, den = (video.get("avg_frame_rate") or "0").partition("/")
    try:
        fps = float(num) / float(den) if den and float(den) else float(num or 0)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    facts = {
        "duration_sec": float(fmt.get("duration") or 0.0),
        "video_codec": video.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "has_audio": has_audio,
        "audio_codec": next(
            (s.get("codec_name") for s in streams if s.get("codec_type") == "audio"),
            None,
        ),
        "path": str(path),
    }
    return facts


def parse_resolution(resolution: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", resolution or "")
    if not m:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid resolution: {resolution!r}. Expected 'WIDTHxHEIGHT'.",
        )
    return int(m.group(1)), int(m.group(2))


def extract_segment(
    src: str | Path,
    dst: str | Path,
    in_sec: float,
    out_sec: float,
    *,
    width: int,
    height: int,
    fps: int,
    sample_rate: int,
) -> None:
    """Trim [in_sec, out_sec) and normalize to the preset in one pass.

    Every segment leaves with identical streams (1 video + 1 audio) so the
    concat demuxer can join them with stream copy. Sources without audio get
    a silent track; aspect mismatches are letterboxed (black).
    """
    if out_sec <= in_sec:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Empty segment: in_sec={in_sec} >= out_sec={out_sec}.",
        )
    info = probe(src)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )
    args = ["-y", "-loglevel", "error", "-i", str(src)]
    if not info["has_audio"]:
        args += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
        audio_map = "1:a:0"
    else:
        audio_map = "0:a:0"
    args += [
        "-ss", f"{in_sec:.3f}",
        "-to", f"{out_sec:.3f}",
        "-vf", vf,
        "-map", "0:v:0",
        "-map", audio_map,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-ar", str(sample_rate),
        "-ac", "2",
        "-shortest",
        str(dst),
    ]
    _run(args, what=f"extract segment [{in_sec:.2f},{out_sec:.2f}) of {src}")
    log.info("segment extracted src=%s dst=%s", src, dst)


def concat_mp4(files: list[str | Path], dst: str | Path) -> None:
    """Join pre-normalized segments (identical codecs) with stream copy."""
    if not files:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Nothing to concat: empty file list.")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as list_file:
        for f in files:
            # Concat demuxer: single-quoted paths, escape embedded quotes.
            safe = str(f).replace("'", "'\\''")
            list_file.write(f"file '{safe}'\n")
        list_path = list_file.name
    try:
        _run(
            ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", str(dst)],
            what=f"concat {len(files)} segments",
        )
    finally:
        os.unlink(list_path)
    log.info("concat done dst=%s segments=%d", dst, len(files))


def extract_frame(src: str | Path, dst: str | Path, at_sec: float = 0.5) -> None:
    """Extract one still frame (used for clip thumbnails)."""
    _run(
        ["-y", "-loglevel", "error", "-ss", f"{at_sec:.3f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "4", str(dst)],
        what=f"extract frame at {at_sec}s of {src}",
    )


def detect_black(path: str | Path, *, at_sec: float, window_sec: float = 0.5) -> bool:
    """True when a black frame is detected inside [at_sec, at_sec+window]."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "info",
         "-ss", str(at_sec), "-t", str(window_sec), "-i", str(path),
         "-vf", "blackdetect=d=0.4:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    return "black_start" in (proc.stderr or "")


def finalize_render(
    inputs: list[str | Path],
    dst: str | Path,
    *,
    filter_complex: str | None = None,
    vmap: str = "0:v",
    amap: str = "0:a",
    fps: float = 30.0,
    sample_rate: int = 48000,
) -> Path:
    """Final render pass: optional filter_complex, then normalized encode.

    Used by services/render for the subtitle burn-in + audio-mix pass.
    `inputs[0]` is the concatenated timeline; the rest are audio clips.
    """
    dst = Path(dst)
    args = ["-y", "-loglevel", "error"]
    for p in inputs:
        args += ["-i", str(p)]
    if filter_complex:
        args += ["-filter_complex", filter_complex]
    args += [
        "-map", vmap, "-map", amap,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-ar", str(sample_rate), "-b:a", "160k",
        str(dst),
    ]
    _run(args, what=f"finalize render -> {dst}")
    return dst
