"""Dubbing lip-sync: fit a TTS/narration audio to a clip length (P1-5, F-13).

Design §4.14 / F-13: after TTS completes, the audio is laid on the clip and
its speed is fine-tuned to the clip length. [가정] allowed range: ±10%
speed adjustment. Beyond that we do NOT stretch (it would sound wrong);
instead we return a warning suggesting clip regeneration or script
shortening, and leave the audio untouched.

Uses the ffmpeg `atempo` filter (0.5–2.0 range; our ±10% band is 0.9–1.1,
well inside it).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.errors import ErrorCode, StudioError
from app.media import ffmpeg_ops

#: ±10% speed-adjustment band (design F-13 [가정]).
MIN_RATE = 0.9
MAX_RATE = 1.1

#: Below this deviation the audio is considered already on-length.
_NOOP_EPS = 0.005


def fit_audio_to_duration(
    src_abs: str | Path,
    target_sec: float,
    dst_abs: str | Path,
) -> dict[str, Any]:
    """Fit audio at `src_abs` to `target_sec` seconds, writing `dst_abs`.

    Returns {"dst_path": str (absolute), "src_duration_sec": float,
    "target_sec": float, "applied_rate": float, "warning": str | None}.

    - |rate - 1| <= 10%: applies `atempo=rate` (re-encode to WAV).
    - Beyond ±10%: copies the source untouched, applied_rate=1.0, and sets
      `warning` (Korean) suggesting clip regeneration / script shortening.
    """
    src = Path(src_abs)
    dst = Path(dst_abs)
    if target_sec <= 0:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "target_sec must be > 0.",
            details={"target_sec": target_sec},
        )
    if not src.exists():
        raise StudioError(
            ErrorCode.NOT_FOUND, "Source audio not found.",
            details={"path": str(src)},
        )
    try:
        facts = ffmpeg_ops.probe(src)
    except Exception as exc:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR, "Could not probe source audio.",
            details={"path": str(src)},
        ) from exc
    src_dur = float(facts.get("duration_sec") or 0)
    if src_dur <= 0:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "Source audio has no measurable duration.",
            details={"path": str(src)},
        )

    rate = src_dur / target_sec
    dst.parent.mkdir(parents=True, exist_ok=True)

    if abs(rate - 1.0) <= _NOOP_EPS:
        shutil.copyfile(src, dst)
        return _result(dst, src_dur, target_sec, 1.0, None)

    if MIN_RATE <= rate <= MAX_RATE:
        proc = subprocess.run(
            [ffmpeg_ops.ffmpeg_bin(), "-y", "-loglevel", "error",
             "-i", str(src), "-filter:a", f"atempo={rate:.6f}", str(dst)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise StudioError(
                ErrorCode.PROVIDER_ERROR, "atempo fitting failed.",
                details={"stderr_tail": proc.stderr[-2000:]},
            )
        return _result(dst, src_dur, target_sec, rate, None)

    # Beyond the ±10% band: do not stretch; warn and keep the original.
    shutil.copyfile(src, dst)
    pct = (rate - 1.0) * 100.0
    if pct > 0:
        hint = (
            f"오디오({src_dur:.1f}s)가 목표 길이({target_sec:.1f}s)보다 "
            f"{pct:.0f}% 깁니다. 대본을 줄이거나 클립을 늘리세요."
        )
    else:
        hint = (
            f"오디오({src_dur:.1f}s)가 목표 길이({target_sec:.1f}s)보다 "
            f"{-pct:.0f}% 짧습니다. 대본을 늘리거나 클립을 줄이세요."
        )
    warning = (
        "립싱크 속도 조정 허용 범위(±10%)를 초과했습니다. "
        "오디오는 조정하지 않았습니다. " + hint
    )
    return _result(dst, src_dur, target_sec, 1.0, warning)


def _result(dst: Path, src_dur: float, target_sec: float,
            rate: float, warning: str | None) -> dict[str, Any]:
    return {
        "dst_path": str(dst),
        "src_duration_sec": round(src_dur, 3),
        "target_sec": round(target_sec, 3),
        "applied_rate": round(rate, 6),
        "warning": warning,
    }
