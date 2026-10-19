"""Timeline render orchestration (F-15, P0-18).

`request_render` validates and enqueues; the Celery worker (tasks_post)
executes `render_timeline`. Render output is a normalized mp4 at
`projects/{project_uuid}/timelines/{timeline_uuid}/output.mp4` (§11.1);
the DB stores the relative path only.

While `status='rendering'` the timeline is read-only: PATCH and a second
render request raise 409 RENDER_LOCKED.

QC gate (ADR-0006 resolved here, design §10.4): a fresh QC report is
required — stale/missing/failing reports raise 409 QC_FAILED; a 'warn'
report requires `ignore_warnings=true`.

Final pass: the concatenated timeline is re-encoded once with the
selected subtitle tracks burned in (ASS via libass, design §10.2) and the
placed BGM/SFX clips mixed underneath (design §4.14). When there is
nothing to burn or mix, the concat output is published as-is.
"""
from __future__ import annotations

import shutil
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.files import immutability, paths
from app.media import ffmpeg_ops


def _get_timeline(db: Session, timeline_id: uuid.UUID):
    from app.models.timeline import Timeline

    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    return timeline


def _get_preset(db: Session, preset_id: uuid.UUID):
    from app.models.system import ExportPreset

    preset = db.get(ExportPreset, preset_id)
    if preset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Export preset not found.")
    return preset


def _require_editable(timeline) -> None:
    if timeline.status == "rendering":
        raise StudioError(
            ErrorCode.RENDER_LOCKED,
            "Timeline is rendering; edits are locked until it finishes.",
            retryable=True,
            details={"timeline_id": str(timeline.id)},
        )


def _validate_clips(db: Session, timeline) -> list[dict[str, Any]]:
    """Return the resolved clip list; raise when a clip is not renderable.

    The cut list is read from `timeline_clips` (ADR-0007) in position order.
    """
    from app.models.generation import Video
    from app.services.timelines import get_clip_order

    clips = get_clip_order(db, timeline.id)
    if not clips:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Timeline has no clips; add clips before rendering.",
        )
    resolved = []
    root = paths.projects_root()
    for i, clip in enumerate(clips):
        video_id = clip.get("video_id")
        in_sec = float(clip.get("in_sec", 0.0))
        out_sec = float(clip.get("out_sec", 0.0))
        if out_sec <= in_sec:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} has an empty range (in_sec >= out_sec).",
                details={"clip_index": i},
            )
        video = db.get(Video, uuid.UUID(str(video_id))) if video_id else None
        if video is None or video.status != "done" or not video.output_path:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} does not reference a finished video.",
                details={"clip_index": i, "video_id": str(video_id)},
            )
        abs_path = root / video.output_path
        if not abs_path.is_file():
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} file is missing on disk.",
                details={"clip_index": i, "output_path": video.output_path},
            )
        resolved.append(
            {
                "index": i,
                "shot_id": clip.get("shot_id"),
                "video_id": str(video.id),
                "src": abs_path,
                "in_sec": in_sec,
                "out_sec": out_sec,
            }
        )
    return resolved


def request_render(
    db: Session,
    timeline_id: uuid.UUID,
    *,
    export_preset_id: uuid.UUID,
    ignore_warnings: bool = False,
) -> dict[str, Any]:
    """Validate and enqueue a render. Returns {"job_id", "estimated_cost_usd"}.

    `job_id` is the timeline id: renders are tracked as timeline state, not
    as generation rows (the render SSE events carry `timeline_id`).
    """
    from app.db.session import dispatch_after_commit

    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)
    preset = _get_preset(db, export_preset_id)
    _validate_clips(db, timeline)  # fail fast before locking

    from app.services import qc_orchestrator

    qc_orchestrator.assert_render_allowed(  # ADR-0006: 409 QC_FAILED
        db, timeline, ignore_warnings=ignore_warnings
    )

    check_enum("render_status", "rendering")
    timeline.status = "rendering"
    timeline.output_preset_id = preset.id
    timeline.error_code = None
    timeline.error_message = None
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.post.render_timeline", str(timeline.id))
    )
    return {"job_id": timeline.id, "estimated_cost_usd": Decimal("0")}


def _finalize_render(db: Session, timeline, preset, concat_abs, tmp_dir, width: int,
                     height: int, fps: float, sample_rate: int):
    """Subtitle burn-in + audio mix pass (design §10.2, §4.14).

    Returns the file to publish: `concat_abs` unchanged when there is
    nothing to burn or mix, otherwise a new re-encoded file.
    """
    from app.media import subtitle_render
    from app.models.audio import AudioGeneration, TimelineAudioClip
    from app.models.subtitle import SubtitleCue, SubtitleTrack
    from app.services import subtitles as subtitle_service

    # --- burn-in cue collection ---
    ass_path = None
    burn = (
        (timeline.subtitle_mode or "none") != "none"
        and bool(getattr(preset, "burn_subtitles", False))
    )
    if burn:
        tracks = []
        for tid in timeline.subtitle_track_ids or []:
            try:
                track = db.get(SubtitleTrack, uuid.UUID(str(tid)))
            except ValueError:
                continue
            if track is not None:
                tracks.append(track)
        tracks.sort(key=lambda t: t.display_order)
        if (timeline.subtitle_mode or "single") == "single":
            tracks = tracks[:1]
        sections: list[tuple[list, dict]] = []
        for track in tracks:
            track_cues = (
                db.query(SubtitleCue)
                .filter(SubtitleCue.track_id == track.id)
                .order_by(SubtitleCue.start_sec)
                .all()
            )
            if not track_cues:
                continue
            sections.append((
                track_cues,
                subtitle_service.get_style(db, timeline.project_id, track.id),
            ))
        if sections:
            # Dual: same-position tracks are separated inside write_ass_sections
            # (later track moves to the opposite position) so the two
            # languages never print on top of each other.
            ass_path = tmp_dir / "subs.ass"
            subtitle_render.write_ass_sections(
                sections, ass_path, width=width, height=height
            )

    # --- audio clip collection ---
    root = paths.projects_root()
    audio_inputs: list[tuple] = []  # (abs_path, start_sec, duration_sec|None, gain_db)
    for clip in (
        db.query(TimelineAudioClip)
        .filter(TimelineAudioClip.timeline_id == timeline.id)
        .order_by(TimelineAudioClip.start_sec)
        .all()
    ):
        audio = db.get(AudioGeneration, clip.audio_id)
        if audio is None or audio.status != "done" or not audio.output_path:
            continue
        abs_path = root / audio.output_path
        if abs_path.is_file():
            audio_inputs.append(
                (abs_path, float(clip.start_sec),
                 float(clip.duration_sec) if clip.duration_sec else (audio.duration_sec or None),
                 float(clip.gain_db or 0.0))
            )

    if ass_path is None and not audio_inputs:
        return concat_abs

    inputs = [str(concat_abs)] + [str(p) for p, _, _, _ in audio_inputs]
    filters: list[str] = []
    vmap, amap = "0:v", "0:a"
    if ass_path is not None:
        esc = (str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'"))
        filters.append(f"[0:v]subtitles='{esc}'[vout]")
        vmap = "[vout]"
    if audio_inputs:
        chains = ["[0:a]aresample=48000[a0]"]
        labels = ["[a0]"]
        for i, (_, start, dur, gain) in enumerate(audio_inputs, start=1):
            ms = int(round(start * 1000))
            trim = f"atrim=0:{dur:.3f}," if dur else ""
            chains.append(
                f"[{i}:a]aresample=48000,{trim}adelay={ms}|{ms},volume={gain}dB[a{i}]"
            )
            labels.append(f"[a{i}]")
        chains.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
        )
        filters.append(";".join(chains))
        amap = "[aout]"

    final_abs = tmp_dir / "final.mp4"
    ffmpeg_ops.finalize_render(
        inputs, final_abs,
        filter_complex=";".join(filters) if filters else None,
        vmap=vmap, amap=amap, fps=fps, sample_rate=sample_rate,
    )
    return final_abs


def render_timeline(timeline_id: str) -> dict[str, Any]:
    """Worker entry: normalize clips -> concat -> write output.mp4.

    Called by the q.post worker with the timeline id (single task arg).
    Returns a summary dict; publishes render.completed on the SSE bridge.
    """
    from app.db.session import session_scope

    with session_scope() as db:
        from app.models.timeline import Timeline

        timeline = db.get(Timeline, uuid.UUID(timeline_id))
        if timeline is None:
            raise StudioError(
                ErrorCode.NOT_FOUND, "Timeline not found.", details={"job_id": timeline_id}
            )
        preset = _get_preset(db, timeline.output_preset_id)
        width, height = ffmpeg_ops.parse_resolution(preset.resolution)
        fps = preset.fps or defaults.OUTPUT_FPS
        sample_rate = defaults.OUTPUT_AUDIO_SAMPLE_RATE

        try:
            clips = _validate_clips(db, timeline)
            root = paths.projects_root()
            out_dir = root / f"projects/{timeline.project_id}/timelines/{timeline.id}"
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir = out_dir / "_segments"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            get_bridge().publish(
                CHANNEL, "job.progress",
                {"job_id": timeline_id, "job_type": "render", "status": "running",
                 "progress_pct": 5.0, "stage": "normalizing"},
            )
            segments: list[str] = []
            total = len(clips)
            for n, clip in enumerate(clips):
                seg = tmp_dir / f"seg_{n:04d}.mp4"
                ffmpeg_ops.extract_segment(
                    clip["src"], seg, clip["in_sec"], clip["out_sec"],
                    width=width, height=height, fps=fps, sample_rate=sample_rate,
                )
                segments.append(str(seg))
                get_bridge().publish(
                    CHANNEL, "job.progress",
                    {"job_id": timeline_id, "job_type": "render", "status": "running",
                     "progress_pct": 5.0 + 80.0 * (n + 1) / total, "stage": "normalizing"},
                )

            get_bridge().publish(
                CHANNEL, "job.progress",
                {"job_id": timeline_id, "job_type": "render", "status": "running",
                 "progress_pct": 90.0, "stage": "concat"},
            )
            final_abs = out_dir / "output.mp4"
            # Immutability: a previous render output is never overwritten or
            # deleted — archive it with a timestamp suffix. output.mp4 always
            # points at the latest render.
            # Render into a temp file first so a failed concat can never leave
            # the timeline without an output.mp4: archive + os.replace only
            # happen after the new file is known good.
            new_abs = out_dir / "output_new.mp4"
            ffmpeg_ops.concat_mp4(segments, new_abs)
            # Subtitle burn-in + audio mix (no-op when there is nothing to
            # burn or mix — concat output is published as-is).
            publish_abs = _finalize_render(
                db, timeline, preset, new_abs, tmp_dir,
                width, height, float(fps), int(sample_rate),
            )
            if publish_abs != new_abs:
                new_abs.unlink()
                publish_abs.rename(new_abs)

            if final_abs.exists():
                stamp = (
                    timeline.updated_at.strftime("%Y%m%dT%H%M%S")
                    if timeline.updated_at
                    else "prev"
                )
                archived = out_dir / f"output_{stamp}.mp4"
                if archived.exists():
                    raise immutability.ImmutableViolation(str(final_abs))
                final_abs.rename(archived)
            new_abs.rename(final_abs)

            # Clean the segment scratch dir (outputs are immutable; segments
            # and intermediate files like subs.ass are not).
            for child in tmp_dir.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
            tmp_dir.rmdir()

            rel_path = paths.assert_relative(
                f"projects/{timeline.project_id}/timelines/{timeline.id}/output.mp4"
            )

            timeline.output_path = rel_path
            check_enum("render_status", "done")
            timeline.status = "done"
            db.flush()
            get_bridge().publish(
                CHANNEL, "render.completed",
                {"timeline_id": timeline_id, "output_path": rel_path, "status": "done"},
            )
            return {"timeline_id": timeline_id, "status": "done", "output_path": rel_path}
        except StudioError as exc:
            check_enum("render_status", "failed")
            timeline.status = "failed"
            timeline.error_code = exc.code
            timeline.error_message = exc.message
            db.flush()
            get_bridge().publish(
                CHANNEL, "render.completed",
                {"timeline_id": timeline_id, "output_path": "", "status": "failed"},
            )
            raise


def fail_timeline_unexpected(timeline_id: str) -> None:
    """Mark a timeline failed after a non-StudioError escaped render_timeline.

    This is the safety net for unexpected crashes (disk full, OSError, ...):
    the render lock must never stick. Publishes render.completed{failed} so
    the UI unlocks; any error here is swallowed (there is nothing left to do).
    """
    from app.db.session import session_scope

    try:
        with session_scope() as db:
            from app.models.timeline import Timeline

            timeline = db.get(Timeline, uuid.UUID(timeline_id))
            if timeline is None:
                return
            check_enum("render_status", "failed")
            timeline.status = "failed"
            timeline.error_code = ErrorCode.PROVIDER_ERROR
            timeline.error_message = "Unexpected render failure; see worker logs."
            db.flush()
            get_bridge().publish(
                CHANNEL, "render.completed",
                {"timeline_id": timeline_id, "output_path": "", "status": "failed"},
            )
    except Exception:  # noqa: BLE001 - last-resort safety net
        pass
