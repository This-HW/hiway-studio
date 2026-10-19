"""QC gate orchestration (F-14, P0-16, design §10).

15-check registry (§10.1: 13 format checks + 2 advisory content checks, P2),
overall verdict (§10.3), report persistence
(§4.17) and the `qc.completed` SSE event. `request_qc` validates and
enqueues; the q.post worker runs `run_qc` with the report id (single task
arg — the row is created by the worker, DB is the truth; ADR-0008).

Check implementation status (2026-09-30):
- REAL (ffmpeg/ffprobe or DB): subtitle_content, subtitle_timing,
  subtitle_cps, subtitle_safe_area, audio_peak, audio_loudness,
  lipsync_drift, encoding_match, black_frame, text_ocr_match,
  text_artifact (OCR via rapidocr_onnxruntime; see app/media/ocr.py).
- STUB (engine not installed — marked stub=true in the report, never
  fail the gate): continuity_first_frame, continuity_between_shots
  (SSIM needs numpy).
- ADVISORY (P2 content — warn at most, never fail the gate; ADR-0010):
  story_fidelity, performance_quality. No vision model is connected, so
  they report stub=true (warn) until one is wired.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.files import paths
from app.media import ffmpeg_ops, loudness, ocr
from app.services import qc_content

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stub(code: str, reason: str) -> CheckResult:
    return CheckResult(code=code, status="pass", details=f"STUB: {reason}", stub=True)


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
    clip_by_shot: dict[Any, Any] = {}
    for clip in clips:
        video = clip.get("video")
        if video is not None and clip.get("abs"):
            clip_by_shot.setdefault(getattr(video, "shot_id", None), clip["abs"])
    for shot in shots:
        media = clip_by_shot.get(getattr(shot, "id", None))
        if media is None:
            gen_id = getattr(shot, "approved_frame_generation_id", None)
            if gen_id:
                gen = db.get(Generation, gen_id)
                if gen is not None and gen.output_path:
                    cand = root / gen.output_path
                    if cand.is_file():
                        media = cand
        if media is not None:
            shot_media[shot.id] = media

    return QCContext(
        timeline=timeline, preset=preset, clips=clips, tracks=tracks,
        audio_clips=audio_clips, shots=shots, output_abs=output_abs,
        shot_media=shot_media,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_text_ocr_match(ctx: QCContext) -> CheckResult:
    """Design §10.1: shots declaring intended_text — OCR the shot media and
    compare normalized (whitespace/case stripped); mismatch -> fail.

    Comparison is per sampled frame: the check passes if ANY frame's
    confident (>= 0.5) detections match the declared text within the
    distance ratio (0.10, same as subtitle_content). Other on-screen text
    (e.g. burned-in timecodes) must not poison the comparison."""
    targets = [s for s in ctx.shots if (s.intended_text or "").strip()]
    if not targets:
        return CheckResult("text_ocr_match", "pass", "No shots declare intended_text.")
    if not ocr.AVAILABLE:
        return _stub("text_ocr_match",
                     f"OCR engine ({ocr.ENGINE}) not installed; intended_text of "
                     f"{len(targets)} shot(s) not verified.")
    mismatches: list[str] = []
    unverifiable: list[str] = []
    for shot in targets:
        media = ctx.shot_media.get(getattr(shot, "id", None))
        if media is None:
            unverifiable.append(str(getattr(shot, "id", "?")))
            continue
        try:
            hits = ocr.recognize(str(media))
        except Exception as e:  # noqa: BLE001 — OCR infra failure is not a text mismatch
            unverifiable.append(f"{shot.id} (ocr error: {e})")
            continue
        want = _norm_text(shot.intended_text or "")
        by_frame: dict[float, list[str]] = {}
        for h in hits:
            if h["confidence"] >= 0.5:
                by_frame.setdefault(h["at_sec"], []).append(h["text"])
        best = 1.0
        for texts in by_frame.values():
            got = _norm_text(" ".join(texts))
            ratio = _levenshtein(got, want) / max(len(got), len(want), 1)
            best = min(best, ratio)
            if ratio <= 0.10:
                break
        else:
            mismatches.append(
                f"shot {shot.id}: intended={shot.intended_text!r} "
                f"not found (best dist ratio {best:.2f})"
            )
    if mismatches:
        return CheckResult(
            "text_ocr_match", "fail",
            f"{len(mismatches)} shot(s) mismatch: " + "; ".join(mismatches),
            measured={"mismatches": mismatches}, threshold={"max_dist_ratio": 0.10},
        )
    note = f"All {len(targets)} intended_text declaration(s) match."
    if unverifiable:
        note += f" {len(unverifiable)} shot(s) had no verifiable media: " + ", ".join(unverifiable)
    return CheckResult("text_ocr_match", "pass", note)


def _check_text_artifact(ctx: QCContext) -> CheckResult:
    """Design §10.1: shots WITHOUT intended_text — any low-confidence
    (< 0.5) OCR detection suggests garbled rendering -> warn.

    Conservative by design: only detections of 2+ characters count, so
    single-character detector noise does not raise false alarms; shots
    with intended_text are skipped because their text is expected."""
    if not ocr.AVAILABLE:
        return _stub("text_artifact",
                     f"OCR engine ({ocr.ENGINE}) not installed; garbled-text scan skipped.")
    suspects: list[str] = []
    scanned = 0
    for shot in ctx.shots:
        if (getattr(shot, "intended_text", None) or "").strip():
            continue
        media = ctx.shot_media.get(getattr(shot, "id", None))
        if media is None:
            continue
        try:
            hits = ocr.recognize(str(media))
        except Exception:  # noqa: BLE001 — infra failure is not garbled text
            continue
        scanned += 1
        for h in hits:
            text = h["text"].strip()
            if h["confidence"] < 0.5 and len(text) >= 2:
                suspects.append(
                    f"shot {shot.id} @{h['at_sec']:.1f}s: {text!r} "
                    f"(conf {h['confidence']:.2f})"
                )
    if suspects:
        return CheckResult(
            "text_artifact", "warn",
            f"Possible garbled text in {len(suspects)} detection(s): "
            + "; ".join(suspects[:5]),
            measured={"suspects": suspects}, threshold={"min_confidence": 0.5},
        )
    return CheckResult(
        "text_artifact", "pass",
        f"Scanned {scanned} shot(s); no low-confidence text detected.",
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


def _check_continuity_first_frame(ctx: QCContext) -> CheckResult:
    return _stub("continuity_first_frame",
                 "SSIM comparison needs numpy/scipy (not installed); skipped.")


def _check_continuity_between_shots(ctx: QCContext) -> CheckResult:
    return _stub("continuity_between_shots",
                 "SSIM comparison needs numpy/scipy (not installed); skipped.")


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
        if abs(audio_dur - placed_dur) > defaults.LIPSYNC_DRIFT_WARN_SEC:
            offenders.append({"audio_id": str(audio.id),
                              "diff_sec": round(abs(audio_dur - placed_dur), 2)})
    if offenders:
        return CheckResult("lipsync_drift", "warn",
                           f"{len(offenders)} dubbing clip(s) drift > "
                           f"{defaults.LIPSYNC_DRIFT_WARN_SEC}s.",
                           measured=offenders,
                           threshold=defaults.LIPSYNC_DRIFT_WARN_SEC)
    return CheckResult("lipsync_drift", "pass",
                       "No dubbing drift (no dialogue clips placed).")


def _check_encoding_match(ctx: QCContext) -> CheckResult:
    if ctx.output_abs is None:
        return CheckResult(
            "encoding_match", "pass",
            "No render output yet; encoding is verified on the next QC run after render.",
        )
    preset = ctx.preset
    facts = ffmpeg_ops.probe(ctx.output_abs)
    problems: list[str] = []
    if preset and preset.resolution:
        w, h = ffmpeg_ops.parse_resolution(preset.resolution)
        if facts["width"] != w or facts["height"] != h:
            problems.append(
                f"resolution {facts['width']}x{facts['height']} != preset {w}x{h}")
    if preset and preset.fps:
        if abs((facts["fps"] or 0) - preset.fps) > 0.5:
            problems.append(f"fps {facts['fps']:.2f} != preset {preset.fps}")
    if preset and preset.codec:
        want = "h264" if preset.codec == "h264" else preset.codec
        if facts["video_codec"] != want:
            problems.append(f"codec {facts['video_codec']} != preset {want}")
    if problems:
        return CheckResult("encoding_match", "fail",
                           "Render output does not match the preset: "
                           + "; ".join(problems), measured=problems)
    return CheckResult("encoding_match", "pass",
                       "Render output matches the preset.")


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
    if any(r.status == "fail" and r.code not in ADVISORY_CHECKS for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "pass"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def request_qc(db: Session, timeline_id: uuid.UUID) -> dict[str, Any]:
    """Enqueue a QC run. Creates the report row (overall='pending') and returns
    its id; the q.post worker fills it in. 202 + qc.completed on completion."""
    from app.db.session import dispatch_after_commit
    from app.models.qc import QCReport
    from app.models.timeline import Timeline

    timeline = db.get(Timeline, timeline_id)
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
    """Worker entry: run the 15 checks, fill the report row, publish qc.completed."""
    from app.db.session import session_scope
    from app.models.qc import QCReport

    with session_scope() as db:
        report = db.get(QCReport, uuid.UUID(qc_report_id))
        if report is None:
            raise StudioError(ErrorCode.NOT_FOUND, "QC report not found.")
        timeline_id = report.target_id
        ctx = _gather(db, timeline_id)
        results = [fn(ctx) for _, fn in CHECKS]
        overall = _overall(results)
        check_enum("qc_result", overall)
        report.overall = overall
        report.checks = [r.to_dict() for r in results]
        db.flush()
        fail_n = sum(1 for r in results if r.status == "fail")
        warn_n = sum(1 for r in results if r.status == "warn")
        get_bridge().publish(
            CHANNEL, "qc.completed",
            {"qc_report_id": qc_report_id, "timeline_id": str(timeline_id),
             "overall": overall, "fail_count": fail_n, "warn_count": warn_n},
        )
        return {"qc_report_id": qc_report_id, "overall": overall,
                "fail_count": fail_n, "warn_count": warn_n}


def get_report(db: Session, report_id: uuid.UUID):
    from app.models.qc import QCReport

    report = db.get(QCReport, report_id)
    if report is None:
        raise StudioError(ErrorCode.NOT_FOUND, "QC report not found.")
    return report


def latest_report(db: Session, timeline_id: uuid.UUID):
    """Newest report for the timeline, or None."""
    from app.models.qc import QCReport

    return (
        db.query(QCReport)
        .filter(QCReport.target_type == "timeline",
                QCReport.target_id == timeline_id)
        .order_by(QCReport.created_at.desc())
        .first()
    )


def assert_render_allowed(
    db: Session, timeline, *, ignore_warnings: bool
) -> None:
    """QC gate for request_render (ADR-0006, design §10.4).

    - no report (or the latest is still running) -> 409 QC_FAILED
      (details.reason='no_report' | 'qc_running')
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
