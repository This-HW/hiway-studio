"""QC gate orchestration (F-14, P0-16, design §10).

15-check registry (§10.1: 13 format checks + 2 advisory content checks, P2),
overall verdict (§10.3), report persistence
(§4.17) and the `qc.completed` SSE event. `request_qc` validates and
enqueues; the q.post worker runs `run_qc` with the report id (single task
arg — the row is created by the worker, DB is the truth; ADR-0008).

Check implementation status (2026-11-09, B3):
- REAL (ffmpeg/ffprobe or DB): subtitle_content, subtitle_timing,
  subtitle_cps, subtitle_safe_area, audio_peak, audio_loudness,
  lipsync_drift, black_frame, text_ocr_match, text_artifact (OCR via
  rapidocr_onnxruntime; see app/media/ocr.py), continuity_first_frame and
  continuity_between_shots (grayscale SSIM via numpy). A continuity check
  that could not measure anything (no ffmpeg, missing clip/still) reports
  warn + stub=true — never pass. The OCR checks likewise: engine not
  installed or nothing verifiable -> warn (unverified); an OCR runtime error
  -> fail; pass only when every target was actually verified.
- NOT APPLICABLE before render (applicable=false, excluded from the
  verdict and the counts): encoding_match. The rendered output is verified
  by the render postflight helpers `postflight_encoding` /
  `postflight_burnin` (plan D5; burn-in needs the subtitle-free reference
  video to ever pass).
- ADVISORY (P2 content — warn at most, never fail the gate; ADR-0010):
  story_fidelity, performance_quality. No vision model is connected, so
  they report stub=true (warn) until one is wired.

Reliability (plan D5): every check runs isolated — an exception in a
required check is recorded as fail, in an advisory check as warn, with
details "CHECK_ERROR: <type>: <msg>". A failure outside the checks
(context building, persistence) marks the report overall='fail' with
error_message in its own committed transaction, so it never stays
'pending'. qc.completed is published only after the commit.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.files import paths
from app.media import ffmpeg_ops, loudness, ocr
from app.services import qc_content

try:  # SSIM backend (declared in pyproject; also a rapidocr dependency)
    import numpy as np
except ImportError:  # pragma: no cover — continuity checks degrade to warn
    np = None

log = logging.getLogger("ai_studio.services.qc")

# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    code: str
    status: str  # pass | warn | fail
    details: str = ""
    measured: Any = None
    threshold: Any = None
    stub: bool = False
    # False = the check does not apply to this run (e.g. encoding_match
    # before render). Non-applicable results never move the verdict and are
    # not counted as pass/warn/fail; `status` is then only a placeholder.
    applicable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unavailable(code: str, reason: str) -> CheckResult:
    """Nothing could be measured (engine or input missing): warn, never pass."""
    return CheckResult(code=code, status="warn", details=f"UNAVAILABLE: {reason}",
                       stub=True)


def _short(exc: BaseException, limit: int = 300) -> str:
    msg = " ".join(str(exc).split()) or "(no message)"
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class QCContext:
    timeline: Any
    preset: Any
    clips: list[dict[str, Any]] = field(default_factory=list)  # resolved videos
    tracks: list[dict[str, Any]] = field(default_factory=list)  # {track, cues, style}
    audio_clips: list[dict[str, Any]] = field(default_factory=list)  # placed clips
    shots: list[Any] = field(default_factory=list)
    output_abs: Any = None  # Path | None
    # shot.id -> absolute media path used by OCR checks: the timeline clip
    # for the shot when present, else the shot's approved frame still.
    shot_media: dict[Any, Any] = field(default_factory=dict)
    # shot.id -> absolute path of the shot's approved still (the file may be
    # missing; the continuity check reports that as unverified).
    shot_stills: dict[Any, Any] = field(default_factory=dict)
    # shot.id -> id of the shot it is continuity-chained from
    # (see _continuity_link).
    continuity_links: dict[Any, Any] = field(default_factory=dict)


def _gather(db: Session, timeline_id: uuid.UUID) -> QCContext:
    from app.models.audio import AudioGeneration, TimelineAudioClip
    from app.models.generation import Generation, Video
    from app.models.production import Shot
    from app.models.subtitle import SubtitleCue, SubtitleTrack
    from app.models.system import ExportPreset
    from app.models.timeline import Timeline
    from app.services import subtitles as subtitle_service
    from app.services.timelines import get_clip_order

    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    preset = db.get(ExportPreset, timeline.output_preset_id) if timeline.output_preset_id else None

    root = paths.projects_root()
    clips: list[dict[str, Any]] = []
    shot_ids: list[uuid.UUID] = []
    for entry in get_clip_order(db, timeline_id):
        video = db.get(Video, uuid.UUID(str(entry["video_id"])))
        if video is None:
            continue
        abs_path = root / video.output_path if video.output_path else None
        clips.append({"video": video, "abs": abs_path,
                      "in_sec": entry["in_sec"], "out_sec": entry["out_sec"]})
        if video.shot_id:
            shot_ids.append(video.shot_id)
    shots = [db.get(Shot, sid) for sid in shot_ids]
    shots = [s for s in shots if s is not None]

    tracks: list[dict[str, Any]] = []
    for tid in timeline.subtitle_track_ids or []:
        try:
            track = db.get(SubtitleTrack, uuid.UUID(str(tid)))
        except ValueError:
            continue
        if track is None:
            continue
        cues = (
            db.query(SubtitleCue)
            .filter(SubtitleCue.track_id == track.id)
            .order_by(SubtitleCue.start_sec)
            .all()
        )
        style = subtitle_service.get_style(db, timeline.project_id, track.id)
        tracks.append({"track": track, "cues": cues, "style": style})

    audio_clips: list[dict[str, Any]] = []
    for clip in (
        db.query(TimelineAudioClip)
        .filter(TimelineAudioClip.timeline_id == timeline_id)
        .order_by(TimelineAudioClip.start_sec)
        .all()
    ):
        audio = db.get(AudioGeneration, clip.audio_id)
        abs_path = root / audio.output_path if audio and audio.output_path else None
        audio_clips.append({"clip": clip, "audio": audio, "abs": abs_path})

    output_abs = None
    if timeline.output_path:
        cand = root / timeline.output_path
        if cand.is_file():
            output_abs = cand

    # Per-shot media for the OCR checks: timeline clip first, approved
    # frame still as fallback (design §10.1: 대상 이미지·클립).
    shot_media: dict[Any, Any] = {}
    shot_stills: dict[Any, Any] = {}
    continuity_links: dict[Any, Any] = {}
    clip_by_shot: dict[Any, Any] = {}
    for clip in clips:
        video = clip.get("video")
        if video is not None and clip.get("abs"):
            clip_by_shot.setdefault(getattr(video, "shot_id", None), clip["abs"])
    for shot in shots:
        gen = None
        gen_id = getattr(shot, "approved_frame_generation_id", None)
        if gen_id:
            gen = db.get(Generation, gen_id)
        if gen is not None and gen.output_path:
            shot_stills[shot.id] = root / gen.output_path
        link = _continuity_link(shot, gen)
        if link is not None:
            continuity_links[shot.id] = link
        media = clip_by_shot.get(getattr(shot, "id", None))
        if media is None:
            still = shot_stills.get(shot.id)
            if still is not None and still.is_file():
                media = still
        if media is not None:
            shot_media[shot.id] = media

    return QCContext(
        timeline=timeline, preset=preset, clips=clips, tracks=tracks,
        audio_clips=audio_clips, shots=shots, output_abs=output_abs,
        shot_media=shot_media, shot_stills=shot_stills,
        continuity_links=continuity_links,
    )


def _continuity_link(shot, approved_gen) -> Any:
    """Id of the shot this shot is continuity-chained from, or None.

    Markers, in order: the explicit `shots.continuity_source_shot_id`
    column, then the approved frame's generation record
    (`params.continuity.used` + `source_shot_id`, written by
    generations.enqueue_frame when the frame was generated with
    use_continuity). Shots that merely sit next to each other are NOT
    chained — a cut between unrelated shots is not a continuity defect.
    """
    explicit = getattr(shot, "continuity_source_shot_id", None)
    if explicit:
        return explicit
    params = getattr(approved_gen, "params", None) or {}
    cont = params.get("continuity") if isinstance(params, dict) else None
    if isinstance(cont, dict) and cont.get("used") and cont.get("source_shot_id"):
        try:
            return uuid.UUID(str(cont["source_shot_id"]))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_text_ocr_match(ctx: QCContext) -> CheckResult:
    """Design §10.1: shots declaring intended_text — OCR the shot media and
    compare normalized (whitespace/case stripped); mismatch -> fail.

    Comparison is per sampled frame: a shot matches if ANY frame's
    confident (>= OCR_CONFIDENCE_LOW) detections match the declared text
    within the distance ratio (0.10, same as subtitle_content). Other
    on-screen text (e.g. burned-in timecodes) must not poison the comparison.

    Verification policy (plan D5 / review WA-H1) — pass only when EVERY
    declaring shot was actually OCR'd and matched:
    - OCR engine not installed -> warn (unverified), never pass.
    - OCR runtime error on any shot -> fail (required check; an errored
      check never passes, even when other shots matched).
    - a declaring shot without media -> warn ("unverified: no media")."""
    code = "text_ocr_match"
    targets = [s for s in ctx.shots if (s.intended_text or "").strip()]
    if not targets:
        return CheckResult(code, "pass", "No shots declare intended_text.")
    if not ocr.AVAILABLE:
        return _unavailable(code,
                            f"OCR engine ({ocr.ENGINE}) not installed; intended_text of "
                            f"{len(targets)} shot(s) not verified.")
    mismatches: list[str] = []
    errors: list[str] = []
    no_media: list[str] = []
    matched = 0
    for shot in targets:
        media = ctx.shot_media.get(getattr(shot, "id", None))
        if media is None:
            no_media.append(str(getattr(shot, "id", "?")))
            continue
        try:
            hits = ocr.recognize(str(media))
        except Exception as e:  # noqa: BLE001 — recorded below as a required-check error
            errors.append(f"shot {shot.id} (ocr error: {_short(e, 160)})")
            continue
        want = _norm_text(shot.intended_text or "")
        by_frame: dict[float, list[str]] = {}
        for h in hits:
            if h["confidence"] >= defaults.OCR_CONFIDENCE_LOW:
                by_frame.setdefault(h["at_sec"], []).append(h["text"])
        best = 1.0
        for texts in by_frame.values():
            got = _norm_text(" ".join(texts))
            ratio = _levenshtein(got, want) / max(len(got), len(want), 1)
            best = min(best, ratio)
            if ratio <= 0.10:
                matched += 1
                break
        else:
            mismatches.append(
                f"shot {shot.id}: intended={shot.intended_text!r} "
                f"not found (best dist ratio {best:.2f})"
            )
    measured = {"targets": len(targets), "matched": matched,
                "mismatches": mismatches, "ocr_errors": errors, "no_media": no_media}
    if mismatches or errors:
        parts = []
        if mismatches:
            parts.append(f"{len(mismatches)} shot(s) mismatch: " + "; ".join(mismatches))
        if errors:
            parts.append(f"{len(errors)} shot(s) could not be OCR'd: " + "; ".join(errors))
        return CheckResult(code, "fail", " | ".join(parts),
                           measured=measured, threshold={"max_dist_ratio": 0.10})
    if no_media or matched != len(targets):
        return CheckResult(
            code, "warn",
            f"unverified: no media for {len(no_media)} of {len(targets)} shot(s) "
            "declaring intended_text (no placed clip or approved still): "
            + ", ".join(no_media),
            measured=measured, threshold={"max_dist_ratio": 0.10})
    return CheckResult(code, "pass",
                       f"All {len(targets)} intended_text declaration(s) match.",
                       measured=measured, threshold={"max_dist_ratio": 0.10})


def _check_text_artifact(ctx: QCContext) -> CheckResult:
    """Design §10.1: shots WITHOUT intended_text — any low-confidence
    (< 0.5) OCR detection suggests garbled rendering -> warn.

    Conservative by design: only detections of 2+ characters count, so
    single-character detector noise does not raise false alarms; shots
    with intended_text are skipped because their text is expected.

    Verification policy (plan D5 / review WA-H1) — pass only when EVERY
    candidate shot was actually scanned:
    - OCR engine not installed -> warn (unverified), never pass.
    - OCR runtime error on any shot -> fail (required check).
    - candidate shots without media (nothing scanned for them) -> warn."""
    code = "text_artifact"
    candidates = [s for s in ctx.shots
                  if not (getattr(s, "intended_text", None) or "").strip()]
    if not candidates:
        return CheckResult(code, "pass", "No shots without intended_text to scan.")
    if not ocr.AVAILABLE:
        return _unavailable(code, f"OCR engine ({ocr.ENGINE}) not installed; "
                                  f"garbled-text scan of {len(candidates)} shot(s) skipped.")
    suspects: list[str] = []
    errors: list[str] = []
    no_media: list[str] = []
    scanned = 0
    for shot in candidates:
        media = ctx.shot_media.get(getattr(shot, "id", None))
        if media is None:
            no_media.append(str(getattr(shot, "id", "?")))
            continue
        try:
            hits = ocr.recognize(str(media))
        except Exception as e:  # noqa: BLE001 — recorded below as a required-check error
            errors.append(f"shot {shot.id} (ocr error: {_short(e, 160)})")
            continue
        scanned += 1
        for h in hits:
            text = h["text"].strip()
            if h["confidence"] < 0.5 and len(text) >= 2:
                suspects.append(
                    f"shot {shot.id} @{h['at_sec']:.1f}s: {text!r} "
                    f"(conf {h['confidence']:.2f})"
                )
    measured = {"candidates": len(candidates), "scanned": scanned,
                "suspects": suspects, "ocr_errors": errors, "no_media": no_media}
    if errors:
        return CheckResult(
            code, "fail",
            f"{len(errors)} shot(s) could not be OCR'd: " + "; ".join(errors[:5]),
            measured=measured, threshold={"min_confidence": 0.5},
        )
    if suspects:
        return CheckResult(
            code, "warn",
            f"Possible garbled text in {len(suspects)} detection(s): "
            + "; ".join(suspects[:5]),
            measured=measured, threshold={"min_confidence": 0.5},
        )
    if no_media or scanned != len(candidates):
        return CheckResult(
            code, "warn",
            f"unverified: no media for {len(no_media)} of {len(candidates)} shot(s); "
            f"scanned {scanned}: " + ", ".join(no_media),
            measured=measured, threshold={"min_confidence": 0.5},
        )
    return CheckResult(
        code, "pass",
        f"Scanned {scanned} shot(s); no low-confidence text detected.",
        measured=measured, threshold={"min_confidence": 0.5},
    )


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm_text(s: str) -> str:
    return "".join(s.split()).lower()


def _check_subtitle_content(ctx: QCContext) -> CheckResult:
    cue_text = " ".join(
        _norm_text(c.text) for t in ctx.tracks for c in t["cues"] if c.text
    )
    src_text = " ".join(
        _norm_text(s.dialogue or "") for s in ctx.shots if (s.dialogue or "").strip()
    )
    if not cue_text or not src_text:
        return CheckResult(
            "subtitle_content", "pass",
            "No cue text or no dialogue source to compare against.",
        )
    dist = _levenshtein(cue_text, src_text)
    ratio = dist / max(len(cue_text), len(src_text))
    if ratio > 0.10:
        return CheckResult(
            "subtitle_content", "warn",
            f"Cue text differs from dialogue by {ratio:.1%} (> 10%).",
            measured=round(ratio, 3), threshold=0.10,
        )
    return CheckResult("subtitle_content", "pass",
                       f"Cue text matches dialogue ({ratio:.1%} diff).",
                       measured=round(ratio, 3), threshold=0.10)


def _check_subtitle_timing(ctx: QCContext) -> CheckResult:
    bad_dur: list[dict] = []
    overlaps: list[dict] = []
    for t in ctx.tracks:
        cues = sorted(t["cues"], key=lambda c: c.start_sec)
        for c in cues:
            dur = c.end_sec - c.start_sec
            if dur < defaults.SUBTITLE_CUE_MIN_SEC or dur > defaults.SUBTITLE_CUE_MAX_SEC:
                bad_dur.append({"start": c.start_sec, "end": c.end_sec,
                                "dur": round(dur, 2)})
        for prev, cur in zip(cues, cues[1:]):
            if cur.start_sec < prev.end_sec:
                overlaps.append({"a": [prev.start_sec, prev.end_sec],
                                 "b": [cur.start_sec, cur.end_sec]})
    if overlaps:
        return CheckResult("subtitle_timing", "fail",
                           f"{len(overlaps)} overlapping cue pair(s).",
                           measured=overlaps)
    if bad_dur:
        return CheckResult(
            "subtitle_timing", "warn",
            f"{len(bad_dur)} cue(s) outside "
            f"{defaults.SUBTITLE_CUE_MIN_SEC}..{defaults.SUBTITLE_CUE_MAX_SEC}s.",
            measured=bad_dur,
            threshold=[defaults.SUBTITLE_CUE_MIN_SEC, defaults.SUBTITLE_CUE_MAX_SEC],
        )
    n = sum(len(t["cues"]) for t in ctx.tracks)
    return CheckResult("subtitle_timing", "pass", f"{n} cue(s) within timing bounds.")


def _check_subtitle_cps(ctx: QCContext) -> CheckResult:
    offenders: list[dict] = []
    for t in ctx.tracks:
        for c in t["cues"]:
            dur = c.end_sec - c.start_sec
            if dur <= 0:
                continue
            cps = len(c.text) / dur
            if cps > defaults.SUBTITLE_CPS_WARN:
                offenders.append({"start": c.start_sec, "cps": round(cps, 1)})
    if offenders:
        return CheckResult("subtitle_cps", "warn",
                           f"{len(offenders)} cue(s) exceed {defaults.SUBTITLE_CPS_WARN} CPS.",
                           measured=offenders, threshold=defaults.SUBTITLE_CPS_WARN)
    return CheckResult("subtitle_cps", "pass", "All cues within CPS budget.")


def _check_subtitle_safe_area(ctx: QCContext) -> CheckResult:
    if ctx.preset and ctx.preset.resolution:
        _, height = ffmpeg_ops.parse_resolution(ctx.preset.resolution)
    else:
        height = defaults.OUTPUT_HEIGHT
    for t in ctx.tracks:
        style = t["style"]
        margin = float(style.get("safe_area_margin_pct", 5.0)) / 100 * height
        block = int(style.get("font_size_px", 48)) * 2  # two-line assumption
        available = height - 2 * margin
        if block > available:
            return CheckResult(
                "subtitle_safe_area", "fail",
                f"Track '{t['track'].label or t['track'].language_code}': subtitle block "
                f"({block}px) exceeds safe area ({available:.0f}px).",
                measured=block, threshold=round(available, 1),
            )
    return CheckResult("subtitle_safe_area", "pass", "Subtitle block fits the safe area.")


# SSIM is measured on grayscale frames resized to a common small size with a
# 7x7 uniform window (the skimage default). Processing parameters, not gate
# thresholds — the thresholds are defaults.QC_SSIM_*.
_SSIM_SIZE = (128, 128)
_SSIM_WIN = 7


def ssim(img_a: Image.Image, img_b: Image.Image) -> float:
    """Mean SSIM of two images (grayscale, resized to _SSIM_SIZE). numpy only."""
    if np is None:
        raise RuntimeError("numpy is not installed")
    x = np.asarray(img_a.convert("L").resize(_SSIM_SIZE, Image.BILINEAR), dtype=np.float64)
    y = np.asarray(img_b.convert("L").resize(_SSIM_SIZE, Image.BILINEAR), dtype=np.float64)
    w = _SSIM_WIN

    def box_mean(m):
        c = np.pad(m, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return (c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w]) / (w * w)

    ux, uy = box_mean(x), box_mean(y)
    norm = (w * w) / (w * w - 1)  # sample covariance
    vx = norm * (box_mean(x * x) - ux * ux)
    vy = norm * (box_mean(y * y) - uy * uy)
    vxy = norm * (box_mean(x * y) - ux * uy)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s_map = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    return float(s_map.mean())


def _extract_frame_file(src: Any, at_sec: float, dst: Any) -> None:
    ffmpeg_ops.extract_frame(src, dst, at_sec=at_sec)


def _clip_frame(path: Any, at_sec: float) -> Image.Image:
    """One decoded frame of a clip at `at_sec`. Raises StudioError/OSError."""
    with tempfile.TemporaryDirectory(prefix="qc_frame_") as d:
        dst = Path(d) / "frame.png"
        _extract_frame_file(path, max(at_sec, 0.0), dst)
        if not dst.is_file() or dst.stat().st_size == 0:
            raise OSError(f"no frame decoded at {at_sec:.2f}s of {path}")
        with Image.open(dst) as img:
            img.load()
            return img.copy()


def _clip_last_frame(clip: dict[str, Any]) -> Image.Image:
    """Last frame of the placed range; backs off when out_sec overshoots."""
    in_sec = float(clip.get("in_sec") or 0.0)
    out_sec = float(clip.get("out_sec") or in_sec)
    last_err: Exception | None = None
    for back in (0.05, 0.5, 1.0):
        try:
            return _clip_frame(clip["abs"], max(out_sec - back, in_sec))
        except (StudioError, OSError) as e:
            last_err = e
    raise OSError(f"last frame unavailable: {last_err}")


def _open_still(path: Any) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        return img.copy()


def _media_missing(path: Any) -> bool:
    return path is None or not Path(path).is_file()


def _continuity_result(code: str, threshold: float, measured: list[dict],
                       low: list[str], unverified: list[str], what: str) -> CheckResult:
    if low or unverified:
        parts = []
        if low:
            parts.append(f"{len(low)} {what} below SSIM {threshold}: " + "; ".join(low[:5]))
        if unverified:
            parts.append(f"unverified {len(unverified)}: " + "; ".join(unverified[:5]))
        prefix = "" if measured else "UNAVAILABLE: "
        return CheckResult(code, "warn", prefix + " | ".join(parts),
                           measured=measured or None, threshold=threshold,
                           stub=not measured)
    return CheckResult(code, "pass",
                       f"All {len(measured)} {what} at or above SSIM {threshold}.",
                       measured=measured, threshold=threshold)


def _check_continuity_first_frame(ctx: QCContext) -> CheckResult:
    """Approved still vs the first frame of the shot's placed clip (§10.1).

    SSIM below QC_SSIM_FIRST_FRAME -> warn. A clip whose still/clip file is
    missing or whose frame cannot be decoded (no ffmpeg) is unverified ->
    warn (stub=true when nothing was measured). t2v clips have no source
    still and are skipped."""
    code = "continuity_first_frame"
    threshold = defaults.QC_SSIM_FIRST_FRAME
    if np is None:
        return _unavailable(code, "numpy not installed; SSIM not measured.")
    if not ctx.clips:
        return _unavailable(code, "unverified: no clips placed on the timeline.")
    measured: list[dict] = []
    low: list[str] = []
    unverified: list[str] = []
    for clip in ctx.clips:
        video = clip.get("video")
        shot_id = getattr(video, "shot_id", None)
        if getattr(video, "mode", None) == "t2v":
            continue
        still = ctx.shot_stills.get(shot_id)
        if still is None:
            unverified.append(f"shot {shot_id}: no approved still")
            continue
        if _media_missing(still):
            unverified.append(f"shot {shot_id}: approved still file missing")
            continue
        if _media_missing(clip.get("abs")):
            unverified.append(f"shot {shot_id}: clip file missing")
            continue
        try:
            score = ssim(_open_still(still),
                         _clip_frame(clip["abs"], float(clip.get("in_sec") or 0.0)))
        except (StudioError, OSError) as e:
            unverified.append(f"shot {shot_id}: frame unavailable ({_short(e, 120)})")
            continue
        measured.append({"shot_id": str(shot_id), "video_id": str(getattr(video, "id", "")),
                         "ssim": round(score, 4)})
        if score < threshold:
            low.append(f"shot {shot_id} ssim {score:.3f}")
    if not measured and not unverified:
        return CheckResult(code, "pass",
                           "Not applicable: every placed clip is t2v (no source still).",
                           threshold=threshold, applicable=False)
    return _continuity_result(code, threshold, measured, low, unverified,
                              "clip first frame(s)")


def _check_continuity_between_shots(ctx: QCContext) -> CheckResult:
    """Last frame of clip N vs first frame of clip N+1, for consecutive clips
    whose shots are continuity-chained (see _continuity_link). Unchained
    neighbours are a deliberate cut and are not compared; with no chained
    pair the check is not applicable. SSIM below QC_SSIM_BETWEEN_SHOTS ->
    warn; missing inputs -> warn (unverified), never pass."""
    code = "continuity_between_shots"
    threshold = defaults.QC_SSIM_BETWEEN_SHOTS
    if not ctx.clips:
        return _unavailable(code, "unverified: no clips placed on the timeline.")
    pairs = []
    for prev, nxt in zip(ctx.clips, ctx.clips[1:]):
        prev_shot = getattr(prev.get("video"), "shot_id", None)
        next_shot = getattr(nxt.get("video"), "shot_id", None)
        link = ctx.continuity_links.get(next_shot)
        if prev_shot is not None and link is not None and str(link) == str(prev_shot):
            pairs.append((prev, nxt, prev_shot, next_shot))
    if not pairs:
        return CheckResult(
            code, "pass",
            "Not applicable: no continuity-chained shot pairs on the timeline "
            "(no SSIM measured).",
            threshold=threshold, stub=True, applicable=False)
    if np is None:
        return _unavailable(code, "numpy not installed; SSIM not measured.")
    measured: list[dict] = []
    low: list[str] = []
    unverified: list[str] = []
    for prev, nxt, prev_shot, next_shot in pairs:
        label = f"{prev_shot}->{next_shot}"
        if _media_missing(prev.get("abs")) or _media_missing(nxt.get("abs")):
            unverified.append(f"{label}: clip file missing")
            continue
        try:
            score = ssim(_clip_last_frame(prev),
                         _clip_frame(nxt["abs"], float(nxt.get("in_sec") or 0.0)))
        except (StudioError, OSError) as e:
            unverified.append(f"{label}: frame unavailable ({_short(e, 120)})")
            continue
        measured.append({"from_shot_id": str(prev_shot), "to_shot_id": str(next_shot),
                         "ssim": round(score, 4)})
        if score < threshold:
            low.append(f"{label} ssim {score:.3f}")
    return _continuity_result(code, threshold, measured, low, unverified,
                              "chained cut(s)")


def _check_audio_peak(ctx: QCContext) -> CheckResult:
    peaks: list[dict] = []
    for ac in ctx.audio_clips:
        if ac["abs"] is None or not ac["abs"].is_file():
            continue
        peak = loudness.measure_peak_db(ac["abs"])
        peaks.append({"audio_id": str(ac["audio"].id), "peak_db": round(peak, 1)})
    if not peaks:
        return CheckResult("audio_peak", "pass", "No audio clips placed.")
    worst = max(peaks, key=lambda p: p["peak_db"])
    if worst["peak_db"] > defaults.QC_AUDIO_PEAK_DBTP:
        return CheckResult("audio_peak", "warn",
                           f"Peak {worst['peak_db']} dB exceeds "
                           f"{defaults.QC_AUDIO_PEAK_DBTP} dBTP (clipping risk).",
                           measured=peaks, threshold=defaults.QC_AUDIO_PEAK_DBTP)
    return CheckResult("audio_peak", "pass",
                       f"Peak {worst['peak_db']} dB within limit.", measured=peaks,
                       threshold=defaults.QC_AUDIO_PEAK_DBTP)


def _check_audio_loudness(ctx: QCContext) -> CheckResult:
    bad: list[dict] = []
    measured: list[dict] = []
    lo = defaults.QC_AUDIO_LUFS_TARGET - defaults.QC_AUDIO_LUFS_TOLERANCE
    hi = defaults.QC_AUDIO_LUFS_TARGET + defaults.QC_AUDIO_LUFS_TOLERANCE
    for ac in ctx.audio_clips:
        if ac["abs"] is None or not ac["abs"].is_file():
            continue
        m = loudness.measure_loudness(ac["abs"])
        measured.append({"audio_id": str(ac["audio"].id),
                         "integrated_lufs": round(m["integrated_lufs"], 1)})
        if not (lo <= m["integrated_lufs"] <= hi):
            bad.append(measured[-1])
    if not measured:
        return CheckResult("audio_loudness", "pass", "No audio clips placed.")
    if bad:
        return CheckResult(
            "audio_loudness", "warn",
            f"{len(bad)} clip(s) outside "
            f"{defaults.QC_AUDIO_LUFS_TARGET}±{defaults.QC_AUDIO_LUFS_TOLERANCE} LUFS.",
            measured=measured,
            threshold=[lo, hi],
        )
    return CheckResult("audio_loudness", "pass",
                       f"All clips within {defaults.QC_AUDIO_LUFS_TARGET}±"
                       f"{defaults.QC_AUDIO_LUFS_TOLERANCE} LUFS.", measured=measured)


def _check_lipsync_drift(ctx: QCContext) -> CheckResult:
    offenders: list[dict] = []
    for ac in ctx.audio_clips:
        audio = ac["audio"]
        if audio is None or audio.kind != "dialogue":
            continue
        clip = ac["clip"]
        audio_dur = audio.duration_sec or 0.0
        placed_dur = clip.duration_sec or audio_dur
        if abs(audio_dur - placed_dur) > defaults.LIPSYNC_WARN_DIFF_SEC:
            offenders.append({"audio_id": str(audio.id),
                              "diff_sec": round(abs(audio_dur - placed_dur), 2)})
    if offenders:
        return CheckResult("lipsync_drift", "warn",
                           f"{len(offenders)} dubbing clip(s) drift > "
                           f"{defaults.LIPSYNC_WARN_DIFF_SEC}s.",
                           measured=offenders,
                           threshold=defaults.LIPSYNC_WARN_DIFF_SEC)
    return CheckResult("lipsync_drift", "pass",
                       "No dubbing drift (no dialogue clips placed).")


def _check_encoding_match(ctx: QCContext) -> CheckResult:
    """Pre-render QC cannot verify the output encoding: the next render's
    output does not exist yet (a previous output belongs to an older timeline
    version). Not applicable here — postflight_encoding verifies the output
    right after render (plan D5)."""
    note = "Not applicable before render: verified at render postflight."
    if ctx.output_abs is not None:
        note += " The previous render output is not re-verified by QC."
    return CheckResult("encoding_match", "pass", note, applicable=False)


# Max |measured fps - preset fps| accepted by postflight_encoding.
_FPS_TOLERANCE = 0.5


def postflight_encoding(output_path: Any, preset: Any) -> CheckResult:
    """Render postflight: ffprobe the output and compare resolution / fps /
    codec with the export preset. Any mismatch or an unreadable output ->
    fail. Returned code: encoding_match."""
    code = "encoding_match"
    if preset is None:
        return CheckResult(code, "pass", "No export preset to compare against.",
                           applicable=False)
    if _media_missing(output_path):
        return CheckResult(code, "fail", f"Render output missing: {output_path}")
    try:
        facts = ffmpeg_ops.probe(output_path)
    except (StudioError, OSError) as e:
        return CheckResult(code, "fail", f"Render output unreadable: {_short(e)}")
    problems: list[str] = []
    want: dict[str, Any] = {}
    if getattr(preset, "resolution", None):
        w, h = ffmpeg_ops.parse_resolution(preset.resolution)
        want["resolution"] = f"{w}x{h}"
        if facts.get("width") != w or facts.get("height") != h:
            problems.append(
                f"resolution {facts.get('width')}x{facts.get('height')} != preset {w}x{h}")
    if getattr(preset, "fps", None):
        want["fps"] = preset.fps
        got_fps = facts.get("fps") or 0
        if abs(got_fps - preset.fps) > _FPS_TOLERANCE:
            problems.append(f"fps {got_fps:.2f} != preset {preset.fps}")
    if getattr(preset, "codec", None):
        want["codec"] = preset.codec
        if facts.get("video_codec") != preset.codec:
            problems.append(f"codec {facts.get('video_codec')} != preset {preset.codec}")
    # duration_sec: the render postflight reuses this one probe for the
    # burn-in cue window (B4B-M1) instead of probing a second time.
    measured = {"width": facts.get("width"), "height": facts.get("height"),
                "fps": facts.get("fps"), "video_codec": facts.get("video_codec"),
                "duration_sec": facts.get("duration_sec")}
    if problems:
        return CheckResult(code, "fail",
                           "Render output does not match the preset: " + "; ".join(problems),
                           measured=measured, threshold=want)
    return CheckResult(code, "pass", "Render output matches the preset.",
                       measured=measured, threshold=want)


def _norm_burnin(s: str) -> str:
    """Case-folded letters/digits only (whitespace and punctuation dropped);
    character order is preserved."""
    return "".join(ch for ch in (s or "").casefold() if ch.isalnum())


def _ordered_similarity(want: str, got: str) -> float:
    """1 - Levenshtein(want, got) / max(len): ORDER-PRESERVING similarity.

    Chosen over difflib's SequenceMatcher ratio because the latter scores a
    rotated phrase too high ('donotgo' vs 'godonot' = 0.71, above the 0.6
    gate); Levenshtein gives 0.43. Extra text in the band lowers the score,
    which errs toward fail, never toward a false pass."""
    if not want and not got:
        return 1.0
    return 1.0 - _levenshtein(want, got) / max(len(want), len(got))


def subtitle_band(width: int, height: int, style: dict | None) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) crop box of the frame region where the
    burn-in renders a cue, from the style's position and safe-area margin.

    Mirrors app.media.subtitle_render._style_line: ASS alignment by position
    (bottom=2 / middle=5 / top=8), MarginV = safe_area_margin_pct of the
    height, font size authored for 1080p and scaled to the render height.
    The band is deliberately generous (>= 30% of the height, room for two
    lines) so wrapped cues and libass rounding still fall inside it."""
    style = style or {}
    position = style.get("position") or "bottom"
    margin = float(style.get("safe_area_margin_pct", 5.0) or 0.0) / 100 * height
    size = max(8, int(round(int(style.get("font_size_px") or 48) * height / 1080)))
    band = min(height, max(int(0.30 * height), int(margin + 3.5 * size)))
    if position == "top":
        return (0, 0, width, band)
    if position == "middle":
        half = max(band // 2, 2 * size)
        center = height // 2
        return (0, max(0, center - half), width, min(height, center + half))
    return (0, height - band, width, height)


def _decode_frame(video: Any, at_sec: float, dst: Path) -> Image.Image:
    """The RGB frame of `video` at `at_sec` (saved next to `dst`).

    Raises OSError/StudioError when no frame can be decoded."""
    frame = dst.with_name(dst.stem + "_frame.png")
    _extract_frame_file(video, at_sec, frame)
    if not frame.is_file() or frame.stat().st_size == 0:
        raise OSError(f"no frame decoded at {at_sec:.2f}s of {Path(str(video)).name}")
    with Image.open(frame) as img:
        return img.convert("RGB")


def _band_ocr(img: Image.Image, style: dict | None, dst: Path) -> str:
    """Crop the subtitle band of `img`, OCR it and return the normalized
    confident text (reading order kept). OCR errors propagate (the caller
    records them as failures)."""
    img.crop(subtitle_band(img.width, img.height, style)).save(dst)
    hits = ocr.recognize(str(dst))
    return _norm_burnin("".join(
        h["text"] for h in hits if h["confidence"] >= defaults.OCR_CONFIDENCE_LOW))


def _scrub_foreign(img: Image.Image, mask: Image.Image | None,
                   fill: Image.Image | None) -> int:
    """Replace, in place, every pixel of `img` under `mask` (another track's
    glyph footprint, mode L, 255 = foreign) with the same pixel of `fill`
    (the subtitle-free reference frame; black without one). Returns the
    number of pixels scrubbed. It removes the other track's pixels only as
    far as `mask` covers them, and the restored reference pixels can show
    background strokes; OCR is not monotonic. See `postflight_burnin` for
    when this is sound (B9-L1)."""
    if mask is None:
        return 0
    if mask.size != img.size:
        raise ValueError(f"foreign mask {mask.size} != frame {img.size}")
    mask = mask.convert("L").point(lambda v: 255 if v else 0)
    scrubbed = mask.histogram()[255]
    if scrubbed:
        if fill is None or fill.size != img.size:
            fill = Image.new("RGB", img.size, "black")
        img.paste(fill, (0, 0), mask)
    return scrubbed


def _cue_field(cue: Any, name: str) -> Any:
    return cue.get(name) if isinstance(cue, dict) else getattr(cue, name, None)


def _sample_sec(cue: Any) -> float:
    """The cue's `sample_sec` (chosen by the render postflight, B9-H1), else
    its midpoint."""
    at = _cue_field(cue, "sample_sec")
    if at is not None:
        return float(at)
    start = float(_cue_field(cue, "start_sec") or 0.0)
    end = float(_cue_field(cue, "end_sec") or start)
    return (start + end) / 2


BURNIN_SYMBOL_ONLY = "symbol_only"


def burnin_symbol_only_unverified(result: CheckResult) -> bool:
    """True for a `postflight_burnin` warn whose ONLY gap is symbol-only cues:
    a reference was compared for every OCR'd cue and every other entry is a
    cue without letters/digits (nothing OCR could compare)."""
    entries = result.measured if isinstance(result.measured, list) else []
    if result.status != "warn" or result.stub or not entries:
        return False
    symbol = [e for e in entries if e.get("unverifiable") == BURNIN_SYMBOL_ONLY]
    others = [e for e in entries if e.get("unverifiable") != BURNIN_SYMBOL_ONLY]
    return bool(symbol) and all("reference_similarity" in e for e in others)


ForeignMask = Callable[[float, tuple[int, int]], "Image.Image | None"]


def postflight_burnin(output_path: Any, cues: Any, *,
                      reference_path: Any = None,
                      style: dict | None = None,
                      min_match: float | None = None,
                      foreign_mask: ForeignMask | None = None) -> CheckResult:
    """Render postflight: burned-in subtitles were really ADDED and are legible.

    For every cue with text, at the cue's sample time (`sample_sec` when the
    cue has one — the render postflight picks it away from the other tracks'
    ASS cue boundaries, B9-H1 — else the midpoint):
    1. crop the subtitle band of the output frame (`subtitle_band`, from
       `style` position / safe-area margin; default bottom) and OCR it;
    2. order-preserving similarity (`_ordered_similarity`) of the
       normalized OCR text vs the cue text must be >= `min_match` (default
       defaults.RENDER_BURNIN_OCR_MIN_MATCH) — else fail (missing /
       transparent / garbled / reordered subtitle);
    3. with `reference_path` (the same video WITHOUT burned subtitles — the
       render pipeline's pre-burn-in intermediate) the same band at the same
       timestamp is OCR'd too; if the cue text already reads there
       (similarity >= min_match), the burn-in cannot be told apart from the
       background -> fail.

    Verdict: any cue failing (2)/(3), an undecodable frame or an OCR runtime
    error -> fail. Without `reference_path` every cue that passes (2) is still
    only "unverified: no reference" -> warn, never pass. OCR engine not
    installed -> warn (unverified). `cues`: objects or dicts with start_sec,
    end_sec, text. Code: burnin_ocr.

    Applicability follows the ORIGINAL cue text: only cues that are blank or
    absent are not applicable. A non-blank cue without letters/digits
    ("...", "…", "♪") is burned in but cannot be compared by OCR: it is
    listed in `measured` with ``unverifiable: "symbol_only"`` and the verdict
    is at most warn (`burnin_symbol_only_unverified` recognises that warn).

    Other tracks (dual subtitles, B9 / ADR-0013): `foreign_mask(at_sec,
    (width, height))` returns the footprint of every OTHER track's glyphs
    that may be shown in the frame sampled at `at_sec` (mode L, 255 =
    foreign; None = nothing). Before the output band is OCR'd those pixels
    are replaced by the reference frame's pixels (`_scrub_foreign`), so
    another track's text does not lower this track's similarity
    (overlapping bands: false fail). It cannot stand in for a transparent /
    missing track ONLY while the mask covers every pixel the other tracks
    changed in that frame: the scrubbed band then equals the reference band,
    and (3) fails a text already readable there. That premise needs the mask
    time to match the frame actually shown (B9-H1: ASS centisecond times,
    real frame PTS — the render postflight's `_sample_time` /
    `_maybe_shown`) and the mask to cover the glyphs (dilated footprint).
    Scrubbing is not "removal only" in general: restored reference pixels
    can reveal background strokes and OCR is not monotonic in removed
    pixels (B9-L1). `measured[].scrubbed_px` records the count."""
    code = "burnin_ocr"
    threshold = (min_match if min_match is not None
                 else defaults.RENDER_BURNIN_OCR_MIN_MATCH)
    present = [(i, c) for i, c in enumerate(cues or [])
               if str(_cue_field(c, "text") or "").strip()]
    if not present:
        return CheckResult(code, "pass", "No subtitle cue text to verify.",
                           threshold=threshold, applicable=False)
    targets = [c for _, c in present if _norm_burnin(_cue_field(c, "text") or "")]
    symbol_only = [(i, c) for i, c in present
                   if not _norm_burnin(_cue_field(c, "text") or "")]
    if targets and not ocr.AVAILABLE:
        return _unavailable(code, f"OCR engine ({ocr.ENGINE}) not installed; "
                                  f"{len(present)} burned-in cue(s) not verified.")
    if _media_missing(output_path):
        return CheckResult(code, "fail", f"Render output missing: {output_path}",
                           threshold=threshold)
    if reference_path is not None and _media_missing(reference_path):
        return CheckResult(code, "fail", f"Reference (pre-burn-in) video missing: "
                                         f"{reference_path}", threshold=threshold)
    measured: list[dict] = []
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="qc_burnin_") as d:
        for i, cue in enumerate(targets):
            mid = _sample_sec(cue)
            want = _norm_burnin(_cue_field(cue, "text"))
            entry: dict[str, Any] = {"cue": i, "at_sec": round(mid, 2)}
            measured.append(entry)
            try:
                out_img = _decode_frame(output_path, mid, Path(d) / f"out_{i}.png")
                ref_img = (None if reference_path is None else
                           _decode_frame(reference_path, mid, Path(d) / f"ref_{i}.png"))
                if foreign_mask is not None:
                    entry["scrubbed_px"] = _scrub_foreign(
                        out_img, foreign_mask(mid, out_img.size), ref_img)
                got = _band_ocr(out_img, style, Path(d) / f"out_{i}.png")
                entry["similarity"] = round(_ordered_similarity(want, got), 3)
                if ref_img is not None:
                    ref = _band_ocr(ref_img, style, Path(d) / f"ref_{i}.png")
                    entry["reference_similarity"] = round(_ordered_similarity(want, ref), 3)
            except Exception as e:  # noqa: BLE001 — frame/OCR failure of a required gate
                entry["error"] = _short(e, 120)
                problems.append(f"cue {i} @{mid:.2f}s: not verifiable ({_short(e, 120)})")
                continue
            if entry["similarity"] < threshold:
                problems.append(f"cue {i} @{mid:.2f}s: {want!r} not legible in the subtitle "
                                f"band (similarity {entry['similarity']:.2f})")
            elif entry.get("reference_similarity", 0.0) >= threshold:
                problems.append(f"cue {i} @{mid:.2f}s: {want!r} already present in the "
                                "reference (no burned-in subtitle distinguishable)")
    for i, cue in symbol_only:
        measured.append({"cue": i, "at_sec": round(_sample_sec(cue), 2),
                         "unverifiable": BURNIN_SYMBOL_ONLY})
    if problems:
        return CheckResult(code, "fail",
                           f"{len(problems)} of {len(targets)} burned-in cue(s) failed "
                           f"(min similarity {threshold:.2f}): " + "; ".join(problems[:5]),
                           measured=measured, threshold=threshold)
    if reference_path is None:
        return CheckResult(code, "warn",
                           f"unverified: no reference (subtitle-free render) — "
                           f"{len(targets)} cue(s) legible in the band but not proven "
                           "to be added by the burn-in.",
                           measured=measured, threshold=threshold)
    if symbol_only:
        return CheckResult(code, "warn",
                           f"unverified: {len(symbol_only)} symbol-only cue(s) (no "
                           "letters/digits to OCR); "
                           f"{len(targets)} other cue(s) legible and absent from the "
                           "reference.",
                           measured=measured, threshold=threshold)
    return CheckResult(code, "pass",
                       f"All {len(targets)} burned-in cue(s) legible and absent "
                       "from the reference.",
                       measured=measured, threshold=threshold)


def _check_black_frame(ctx: QCContext) -> CheckResult:
    hits: list[dict] = []
    for c in ctx.clips:
        if c["abs"] is None or not c["abs"].is_file():
            continue
        dur = max(c["out_sec"] - c["in_sec"], 0.6)
        for at in (0.0, max(dur - 0.5, 0.0)):
            if ffmpeg_ops.detect_black(c["abs"], at_sec=at, window_sec=0.5):
                hits.append({"video_id": str(c["video"].id), "at_sec": at})
                break
    if hits:
        return CheckResult("black_frame", "warn",
                           f"{len(hits)} clip(s) start/end with a black frame.",
                           measured=hits)
    return CheckResult("black_frame", "pass", "No black frames at clip edges.")


CHECKS: list[tuple[str, Any]] = [
    ("text_ocr_match", _check_text_ocr_match),
    ("text_artifact", _check_text_artifact),
    ("subtitle_content", _check_subtitle_content),
    ("subtitle_timing", _check_subtitle_timing),
    ("subtitle_cps", _check_subtitle_cps),
    ("subtitle_safe_area", _check_subtitle_safe_area),
    ("continuity_first_frame", _check_continuity_first_frame),
    ("continuity_between_shots", _check_continuity_between_shots),
    ("audio_peak", _check_audio_peak),
    ("audio_loudness", _check_audio_loudness),
    ("lipsync_drift", _check_lipsync_drift),
    ("encoding_match", _check_encoding_match),
    ("black_frame", _check_black_frame),
    # P2 advisory content checks (ADR-0010): warn at most, never fail.
    ("story_fidelity", qc_content.check_story_fidelity),
    ("performance_quality", qc_content.check_performance_quality),
]

assert len(CHECKS) == 15, "design §10.1 requires 13 format checks + 2 advisory content checks"

# Advisory (P2) content checks: they may warn but can never fail the gate
# (ADR-0010). _overall excludes them from the fail branch as a hard guarantee,
# on top of the check functions never emitting "fail" themselves.
ADVISORY_CHECKS: set[str] = {"story_fidelity", "performance_quality"}


def _overall(results: list[CheckResult]) -> str:
    # Non-applicable checks never move the verdict. (Advisory results are
    # normalized to pass|warn by _run_check before they get here.)
    applicable = [r for r in results if getattr(r, "applicable", True)]
    if any(r.status == "fail" and r.code not in ADVISORY_CHECKS for r in applicable):
        return "fail"
    if any(r.status == "warn" for r in applicable):
        return "warn"
    return "pass"


def count_results(results: list[CheckResult]) -> dict[str, int]:
    """pass / warn / fail counts over applicable checks + not_applicable."""
    counts = {"pass": 0, "warn": 0, "fail": 0, "not_applicable": 0}
    for r in results:
        if not getattr(r, "applicable", True):
            counts["not_applicable"] += 1
        elif r.status in counts:
            counts[r.status] += 1
    return counts


def _run_check(code: str, fn: Any, ctx: QCContext) -> CheckResult:
    """Run one check in isolation. An exception never yields pass: required
    check -> fail, advisory check -> warn, details "CHECK_ERROR: ..."."""
    try:
        result = fn(ctx)
        if not isinstance(result, CheckResult):
            raise TypeError(f"check returned {type(result).__name__}, not CheckResult")
        if result.status not in ("pass", "warn", "fail"):
            raise ValueError(f"check returned unknown status {result.status!r}")
    except Exception as exc:  # noqa: BLE001 — isolation boundary, recorded in the report
        log.exception("qc check crashed code=%s", code)
        return CheckResult(code, "warn" if code in ADVISORY_CHECKS else "fail",
                           f"CHECK_ERROR: {type(exc).__name__}: {_short(exc)}")
    if code in ADVISORY_CHECKS and result.status == "fail":
        result.status = "warn"  # ADR-0010 hard guarantee
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def request_qc(db: Session, timeline_id: uuid.UUID) -> dict[str, Any]:
    """Enqueue a QC run. Creates the report row (overall='pending') and returns
    its id; the q.post worker fills it in. 202 + qc.completed on completion.

    B4R3-H1: the timeline row is locked first (SELECT ... FOR NO KEY UPDATE;
    on SQLite lock_row takes the database write lock up front, B4R4-M2), so
    report creation is serialized per timeline and with content edits: the row's `seq` (max + 1, assigned on
    insert) is the commit order, and `timeline_content_version` is the
    version current at that point."""
    from app.db.session import dispatch_after_commit
    from app.models.qc import QCReport
    from app.models.timeline import Timeline
    from app.services.generations import lock_row

    timeline = lock_row(db, Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    report = QCReport(
        project_id=timeline.project_id,
        target_type="timeline",
        target_id=timeline.id,
        overall="pending",  # transient; worker overwrites with pass|warn|fail
        checks=[],
        timeline_content_version=timeline.content_version or 0,
    )
    check_enum("qc_target", "timeline")
    db.add(report)
    db.flush()
    report_id = str(report.id)
    dispatch_after_commit(
        db, lambda: dispatch_job("q.post.run_qc", report_id)
    )
    return {"qc_report_id": report_id}


def run_qc(qc_report_id: str) -> dict[str, Any]:
    """Worker entry: run the 15 checks, fill the report row, publish qc.completed.

    The report is committed before qc.completed is published. If anything
    outside the per-check isolation fails, the report is marked
    overall='fail' (+ error_message) in a separate committed transaction —
    it never stays 'pending' — and the original exception is re-raised."""
    try:
        summary = _execute_qc(qc_report_id)
    except Exception as exc:
        log.exception("qc run failed qc_report_id=%s", qc_report_id)
        fail_report_unexpected(qc_report_id, exc)
        raise
    _publish_completed(summary)
    return summary


def _execute_qc(qc_report_id: str) -> dict[str, Any]:
    from app.db.session import session_scope
    from app.models.qc import QCReport

    with session_scope() as db:
        report = db.get(QCReport, uuid.UUID(str(qc_report_id)))
        if report is None:
            raise StudioError(ErrorCode.NOT_FOUND, "QC report not found.")
        timeline_id = report.target_id
        ctx = _gather(db, timeline_id)
        results = [_run_check(code, fn, ctx) for code, fn in CHECKS]
        overall = _overall(results)
        check_enum("qc_result", overall)
        report.overall = overall
        report.checks = [r.to_dict() for r in results]
        report.error_message = None
        db.flush()
        counts = count_results(results)
    # session_scope committed on exit
    return {"qc_report_id": str(qc_report_id), "timeline_id": str(timeline_id),
            "overall": overall, "fail_count": counts["fail"],
            "warn_count": counts["warn"], "pass_count": counts["pass"],
            "not_applicable_count": counts["not_applicable"]}


def _publish_completed(summary: dict[str, Any]) -> None:
    """qc.completed (contract fields only). Called after the commit; a broker
    failure is logged, not raised — the committed row is the truth."""
    try:
        get_bridge().publish(
            CHANNEL, "qc.completed",
            {"qc_report_id": summary["qc_report_id"],
             "timeline_id": summary["timeline_id"], "overall": summary["overall"],
             "fail_count": summary["fail_count"], "warn_count": summary["warn_count"]},
        )
    except Exception:  # noqa: BLE001
        log.exception("qc.completed publish failed qc_report_id=%s",
                      summary.get("qc_report_id"))


def fail_report_unexpected(qc_report_id: str, exc: BaseException) -> bool:
    """Mark a still-pending report overall='fail' with error_message, in its
    own short committed transaction (survives the caller's rollback), then
    publish qc.completed. Returns False when the row is missing or already
    terminal (a finished report is never overwritten)."""
    from app.db.session import session_scope
    from app.models.qc import QCReport

    message = f"QC_RUN_ERROR: {type(exc).__name__}: {_short(exc, 1000)}"
    try:
        rid = uuid.UUID(str(qc_report_id))
    except ValueError:
        return False
    try:
        with session_scope() as db:
            updated = (
                db.query(QCReport)
                .filter(QCReport.id == rid, QCReport.overall == "pending")
                .update({QCReport.overall: "fail", QCReport.error_message: message},
                        synchronize_session=False)
            )
            report = db.get(QCReport, rid) if updated else None
            timeline_id = str(report.target_id) if report is not None else ""
    except Exception:  # noqa: BLE001 — DB itself unavailable; nothing more to do
        log.exception("could not persist qc failure qc_report_id=%s", qc_report_id)
        return False
    if not updated:
        return False
    _publish_completed({"qc_report_id": str(rid), "timeline_id": timeline_id,
                        "overall": "fail", "fail_count": 0, "warn_count": 0})
    return True


def get_report(db: Session, report_id: uuid.UUID):
    from app.models.qc import QCReport

    report = db.get(QCReport, report_id)
    if report is None:
        raise StudioError(ErrorCode.NOT_FOUND, "QC report not found.")
    return report


def latest_report(db: Session, timeline_id: uuid.UUID):
    """Newest report for the timeline, or None.

    Ordered by the per-timeline issue number `seq` (B4R3-H1), never by
    created_at: equal timestamps must not let an older pass/warn hide a newer
    pending/fail. The single source of "latest" for the render gate and the
    GET /timelines/{id}/qc-latest endpoint. A latest report with
    `order_ambiguous` (legacy tie, B4R4-H1) is returned as is; the render
    gate refuses it."""
    from app.models.qc import QCReport

    return (
        db.query(QCReport)
        .filter(QCReport.target_type == "timeline",
                QCReport.target_id == timeline_id)
        .order_by(QCReport.seq.desc())
        .first()
    )


def assert_render_allowed(
    db: Session, timeline, *, ignore_warnings: bool
) -> None:
    """QC gate for request_render (ADR-0006, design §10.4).

    - no report (or the latest is still running) -> 409 QC_FAILED
      (details.reason='no_report' | 'qc_running')
    - latest is a legacy report of unknown issue order (order_ambiguous,
      B4R4-H1) -> 409 QC_FAILED (reason='stale', order_ambiguous=true):
      never authorizes, whatever its result; a fresh QC is required
    - report older than the timeline content -> 409 QC_FAILED (reason='stale')
    - overall='fail' -> 409 QC_FAILED
    - overall='warn' without ignore_warnings -> 409 QC_FAILED
      (details.reason='warnings_not_ignored')
    """
    report = latest_report(db, timeline.id)
    if report is None:
        raise StudioError(
            ErrorCode.QC_FAILED,
            "Render requires a QC report. Run QC first.",
            details={"reason": "no_report", "timeline_id": str(timeline.id)},
        )
    if report.order_ambiguous:
        # Checked before 'pending': a legacy pending row may never finish.
        raise StudioError(
            ErrorCode.QC_FAILED,
            "The latest QC report predates reliable report ordering and tied "
            "with another report; run QC again before rendering.",
            details={"reason": "stale", "qc_report_id": str(report.id),
                     "order_ambiguous": True},
        )
    if report.overall == "pending":
        raise StudioError(
            ErrorCode.QC_FAILED,
            "QC is still running. Wait for qc.completed, then render.",
            details={"reason": "qc_running", "qc_report_id": str(report.id)},
        )
    if (report.timeline_content_version or 0) != (timeline.content_version or 0):
        raise StudioError(
            ErrorCode.QC_FAILED,
            "QC report is stale: the timeline changed after the QC run.",
            details={"reason": "stale", "qc_report_id": str(report.id)},
        )
    if report.overall == "fail":
        raise StudioError(
            ErrorCode.QC_FAILED,
            "QC failed: fix the failing checks before rendering.",
            details={"reason": "failed", "qc_report_id": str(report.id)},
        )
    if report.overall == "warn" and not ignore_warnings:
        raise StudioError(
            ErrorCode.QC_FAILED,
            "QC reported warnings: re-render with ignore_warnings=true to proceed.",
            details={"reason": "warnings_not_ignored", "qc_report_id": str(report.id)},
        )
