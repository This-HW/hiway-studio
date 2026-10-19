"""Content QC checks (P2, advisory — ADR-0010, D-06).

story_fidelity: does the shot's approved frame follow the shot prompt/story?
performance_quality: is the acting in the video clip natural (expression, motion)?

Both are ADVISORY: they may emit warn at most, never fail, so they can never
block the render gate (see ADVISORY_CHECKS in qc_orchestrator).

Vision backend: app.services.vision_client — a generic OpenAI-compatible
chat-completions client (any endpoint: OpenAI, Ollama /v1, vLLM, LM Studio,
...). No vendor lock-in: the model is chosen purely by env config.

Stub boundary (P0 pattern): without a configured vision endpoint
(QC_VISION_BASE_URL / QC_VISION_MODEL unset) no real judgement is possible, so
the checks report stub=true with status warn — never hidden as an auto-pass.
With an endpoint configured: score < threshold -> warn, else pass.
Vision/client errors degrade to warn as well — advisory checks must never
block the gate, even when the model misbehaves.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from app.core import defaults
from app.services import vision_client

# Codes are the CHECKS registry keys in qc_orchestrator.
STORY_FIDELITY = "story_fidelity"
PERFORMANCE_QUALITY = "performance_quality"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

_STORY_SYSTEM_PROMPT = (
    "You are a film continuity judge. You are given still image(s) from a shot "
    "plus the shot's intended action and dialogue. Rate 0-100 how faithfully "
    "the image follows the described story (subject, action, composition, mood). "
    "Respond with ONLY a JSON object: "
    '{"score": <integer 0-100>, "notes": "<one or two sentences>"}'
)
_PERFORMANCE_SYSTEM_PROMPT = (
    "You are a film acting critic. You are given frames sampled from a video "
    "clip. Rate 0-100 how natural the acting looks (facial expression, body "
    "motion, no uncanny or garbled artifacts). "
    "Respond with ONLY a JSON object: "
    '{"score": <integer 0-100>, "notes": "<one or two sentences>"}'
)


def _stub_result(code: str, what: str) -> Any:
    # Local import: qc_orchestrator imports this module, so a top-level
    # import would be circular (this file's own deferred-import pattern
    # matches the one used inside qc_orchestrator._gather).
    from app.services.qc_orchestrator import CheckResult

    return CheckResult(
        code=code,
        status="warn",
        details=(
            f"STUB: vision 모델 미연결 — {what} 내용 검수를 수행할 수 없습니다. "
            "환경변수 QC_VISION_BASE_URL과 QC_VISION_MODEL을 설정하면 "
            "실제 판정이 동작합니다 (OpenAI-호환 엔드포인트)."
        ),
        threshold=0.0,
        stub=True,
    )


def _story_context(shot: Any) -> str:
    action = (getattr(shot, "action", None) or "").strip()
    dialogue = (getattr(shot, "dialogue", None) or "").strip()
    lines = []
    if action:
        lines.append(f"Intended action: {action}")
    if dialogue:
        lines.append(f"Dialogue: {dialogue}")
    return "\n".join(lines)


def _downscale(src: Path, dst: Path) -> Path:
    """Downscale to QC_VISION_FRAME_MAX_DIM and save as JPEG (small base64)."""
    max_dim = defaults.QC_VISION_FRAME_MAX_DIM
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        im.save(dst, "JPEG", quality=80)
    return dst


def _shot_frames(
    ctx: Any, shot: Any, fractions: tuple[float, ...], tmpdir: Path
) -> list[Path]:
    """Representative frame(s) for one shot, downscaled JPEGs in tmpdir.

    Still image -> the image itself. Video -> frames at the given duration
    fractions (design: middle frame for story; spread frames for acting).
    Missing/unreadable media -> [] (shot is skipped, not failed).
    """
    from app.media import ffmpeg_ops

    media = ctx.shot_media.get(getattr(shot, "id", None))
    if media is None:
        return []
    p = Path(media)
    if not p.is_file():
        return []
    sid = str(getattr(shot, "id", "?"))[:8]
    if p.suffix.lower() in _IMAGE_EXTS:
        try:
            return [_downscale(p, tmpdir / f"{sid}_still.jpg")]
        except Exception:  # noqa: BLE001 — unreadable image: skip the shot
            return []
    try:
        info = ffmpeg_ops.probe(str(p))
        duration = float(info.get("duration_sec") or 0)
    except Exception:  # noqa: BLE001 — unprobable video: skip the shot
        return []
    if duration <= 0:
        return []
    out: list[Path] = []
    for i, frac in enumerate(fractions):
        dst = tmpdir / f"{sid}_{i}.jpg"
        try:
            ffmpeg_ops.extract_frame(str(p), dst, at_sec=duration * frac)
            out.append(_downscale(dst, dst))
        except Exception:  # noqa: BLE001 — bad frame: skip it, keep the rest
            continue
    return out


def _run_advisory_check(
    ctx: Any,
    code: str,
    system_prompt: str,
    user_text_fn: Any,
    fractions: tuple[float, ...],
    threshold: float,
    kind_label: str,
    needs_context: bool,
) -> Any:
    """Score every shot with media; worst score drives the advisory verdict.

    Emits only "warn" or "pass" — never "fail" (hard advisory invariant, on
    top of the ADVISORY_CHECKS double-guarantee in qc_orchestrator._overall).
    """
    from app.services.qc_orchestrator import CheckResult

    shots = list(getattr(ctx, "shots", None) or [])
    if not shots:
        return CheckResult(code, "warn", "No shots to judge.", stub=False)

    worst = 1.0
    per_shot: list[str] = []
    problems: list[str] = []
    scored = 0
    with tempfile.TemporaryDirectory(prefix="qc_vision_") as tmp:
        tmpdir = Path(tmp)
        for shot in shots:
            sid = str(getattr(shot, "id", "?"))[:8]
            context = user_text_fn(shot)
            if needs_context and not context:
                continue  # nothing to judge against — skip, not a failure
            frames = _shot_frames(ctx, shot, fractions, tmpdir)
            if not frames:
                continue  # no verifiable media — skip, not a failure
            try:
                vs = vision_client.score_shot(
                    frames,
                    system_prompt=system_prompt,
                    user_text=context or "Rate the attached frame(s).",
                )
            except Exception as e:  # noqa: BLE001 — VisionError or media error:
                # advisory degrades; the gate must not be blocked by QC infra.
                problems.append(f"shot {sid}: {e}")
                continue
            scored += 1
            s01 = vs.score / 100.0
            worst = min(worst, s01)
            note = f"shot {sid}: {vs.score}/100"
            if vs.notes:
                note += f" — {vs.notes}"
            per_shot.append(note)

    if scored == 0:
        detail = "No shot could be vision-scored (no verifiable media"
        if problems:
            detail += f"; {len(problems)} scoring error(s): " + "; ".join(problems[:3])
        return CheckResult(code, "warn", detail + ").", stub=False)

    detail = (
        f"{kind_label}: worst {worst:.2f} over {scored} scored shot(s). "
        + " | ".join(per_shot[:5])
    )
    if problems:
        detail += f" ({len(problems)} shot(s) skipped: " + "; ".join(problems[:2]) + ")"
    if worst < threshold:
        return CheckResult(
            code, "warn", detail,
            measured=round(worst, 3), threshold=threshold, stub=False,
        )
    return CheckResult(
        code, "pass", detail,
        measured=round(worst, 3), threshold=threshold, stub=False,
    )


def check_story_fidelity(ctx: Any) -> Any:
    """Advisory: approved frame vs shot action/dialogue fidelity."""
    if not vision_client.available():
        return _stub_result(STORY_FIDELITY, "스토리 충실도")
    return _run_advisory_check(
        ctx,
        STORY_FIDELITY,
        _STORY_SYSTEM_PROMPT,
        _story_context,
        fractions=(0.5,),  # representative middle frame
        threshold=defaults.QC_STORY_FIDELITY_WARN_SCORE,
        kind_label="Story fidelity",
        needs_context=True,
    )


def check_performance_quality(ctx: Any) -> Any:
    """Advisory: acting quality (expression/motion naturalness) of clips."""
    if not vision_client.available():
        return _stub_result(PERFORMANCE_QUALITY, "연기 품질")
    return _run_advisory_check(
        ctx,
        PERFORMANCE_QUALITY,
        _PERFORMANCE_SYSTEM_PROMPT,
        _story_context,  # extra context when present; not required
        fractions=(0.25, 0.5, 0.75),  # spread frames capture motion/expression
        threshold=defaults.QC_PERFORMANCE_WARN_SCORE,
        kind_label="Performance quality",
        needs_context=False,
    )
