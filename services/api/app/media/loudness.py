"""Audio loudness / peak measurement via ffmpeg (QC audio_peak, audio_loudness).

- `measure_loudness`: EBU R128 integrated loudness + true peak, using the
  `loudnorm` filter in measurement mode (`print_format=json`) on a null
  output — no normalization is applied to the source.
- `measure_peak`: sample peak dB via `astats` metadata (fast path for
  short files). True-peak is approximated by the loudnorm true-peak value.

Never imports services (ARCHITECTURE §15).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app.media.ffmpeg_ops import FFmpegError, ffmpeg_bin
from app.core.errors import ErrorCode


def _run_ffmpeg(args: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_bin(), *args], capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffmpeg binary not found (FFMPEG_BIN={ffmpeg_bin()!r}).",
            details={"what": what},
        ) from exc
    if proc.returncode != 0:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            f"ffmpeg failed: {what}.",
            details={"what": what, "stderr_tail": proc.stderr[-2000:]},
        )
    return proc


def measure_loudness(path: str | Path) -> dict:
    """Return {integrated_lufs, true_peak_dbtp, lra} for an audio file."""
    proc = _run_ffmpeg(
        [
            "-hide_banner", "-i", str(path),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ],
        what=f"loudness measure {path}",
    )
    # The JSON block is printed to stderr at the end (keys: input_i/input_tp/...).
    match = re.search(r"\{[\s\S]*\"input_i\"[\s\S]*\}", proc.stderr)
    if not match:
        raise FFmpegError(
            ErrorCode.PROVIDER_ERROR,
            "loudnorm produced no JSON summary.",
            details={"what": f"loudness measure {path}"},
        )
    data = json.loads(match.group(0))
    return {
        "integrated_lufs": float(data["input_i"]),
        "true_peak_dbtp": float(data["input_tp"]),
        "lra": float(data["input_lra"]),
    }


def measure_peak_db(path: str | Path) -> float:
    """Return the sample peak level in dB (astats). -inf when silent."""
    proc = _run_ffmpeg(
        ["-hide_banner", "-i", str(path), "-af", "astats=metadata=1", "-f", "null", "-"],
        what=f"peak measure {path}",
    )
    peaks = re.findall(r"Peak level dB:\s*(-?[\d.]+|-inf)", proc.stderr)
    vals = [float(p) for p in peaks if p != "-inf"]
    if not vals:
        return float("-inf")
    return max(vals)
