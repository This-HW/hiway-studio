"""Timeline assembly and editing (F-12, P0-14).

The cut list is the normalized `timeline_clips` table (ADR-0007, D-12); the
API exposes it as the F-12 `clip_order` array — assembled in `position`
order on read, atomically replaced on write. While `status='rendering'`
the timeline is read-only (409 RENDER_LOCKED) — the UI mirrors this by
locking the editor until `render.completed` arrives.

Audio-clip placement and subtitle tracks arrive in Phase 4; this module
only manages the video cut list.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.services import videos as video_service
from app.services.render import _require_editable


def _get_project(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def _get_timeline(db: Session, timeline_id: uuid.UUID):
    from app.models.timeline import Timeline

    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    return timeline


def get_clip_order(db: Session, timeline_id: uuid.UUID) -> list[dict[str, Any]]:
    """Assemble the F-12 clip_order array from timeline_clips (position order)."""
    from app.models.timeline import TimelineClip

    rows = (
        db.query(TimelineClip)
        .filter(TimelineClip.timeline_id == timeline_id)
        .order_by(TimelineClip.position)
        .all()
    )
    return [
        {
            "shot_id": str(r.shot_id),
            "video_id": str(r.video_id),
            "in_sec": r.in_sec,
            "out_sec": r.out_sec,
        }
        for r in rows
    ]


def _set_clips(
    db: Session, timeline_id: uuid.UUID, entries: list[dict[str, Any]]
) -> None:
    """Atomically replace the timeline's clips (validated entries, in order)."""
    from app.models.timeline import TimelineClip

    validated = [
        _validate_clip_entry(db, entry, i) for i, entry in enumerate(entries)
    ]
    db.query(TimelineClip).filter(TimelineClip.timeline_id == timeline_id).delete()
    for position, clip in enumerate(validated):
        db.add(
            TimelineClip(
                timeline_id=timeline_id,
                position=position,
                shot_id=uuid.UUID(clip["shot_id"]),
                video_id=uuid.UUID(clip["video_id"]),
                in_sec=clip["in_sec"],
                out_sec=clip["out_sec"],
            )
        )
    db.flush()


def list_timelines(db: Session, project_id: uuid.UUID) -> list:
    from app.models.timeline import Timeline

    _get_project(db, project_id)
    return (
        db.query(Timeline)
        .filter(Timeline.project_id == project_id)
        .order_by(Timeline.created_at.desc())
        .all()
    )


def create_timeline(db: Session, project_id: uuid.UUID, *, name: str | None = None):
    from app.models.timeline import Timeline

    _get_project(db, project_id)
    timeline = Timeline(
        project_id=project_id,
        name=(name or "").strip() or "기본 타임라인",
    )
    db.add(timeline)
    db.flush()
    return timeline


def _validate_clip_entry(db: Session, entry: dict[str, Any], index: int) -> dict[str, Any]:
    from app.models.production import Shot
    from app.models.generation import Video

    try:
        shot_id = uuid.UUID(str(entry["shot_id"]))
        video_id = uuid.UUID(str(entry["video_id"]))
    except (KeyError, ValueError, AttributeError) as exc:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Clip #{index} needs shot_id and video_id as UUIDs.",
            details={"clip_index": index},
        ) from exc
    in_sec = float(entry.get("in_sec", 0.0) or 0.0)
    out_sec = float(entry.get("out_sec", 0.0) or 0.0)
    if out_sec <= in_sec:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Clip #{index} has an empty range (in_sec >= out_sec).",
            details={"clip_index": index},
        )
    shot = db.get(Shot, shot_id)
    video = db.get(Video, video_id)
    if shot is None or video is None or video.shot_id != shot.id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Clip #{index} references an unknown shot/video pair.",
            details={"clip_index": index},
        )
    if video.status != "done" or not video.output_path:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Clip #{index} video is not finished.",
            details={"clip_index": index, "video_status": video.status},
        )
    # Clamp the range to the actual clip length.
    out_sec = min(out_sec, video.duration_sec)
    if out_sec <= in_sec:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Clip #{index} range is outside the video duration.",
            details={"clip_index": index, "video_duration_sec": video.duration_sec},
        )
    return {
        "shot_id": str(shot_id),
        "video_id": str(video_id),
        "in_sec": in_sec,
        "out_sec": out_sec,
    }


def _mark_edited(timeline) -> None:
    # Editing a finished timeline returns it to draft (re-render required).
    if timeline.status == "done":
        check_enum("render_status", "draft")
        timeline.status = "draft"


def bump_content_version(db: Session, timeline_id: uuid.UUID) -> int:
    """Bump the render-content version (design §10.4 staleness gate).

    Call after every edit that changes what a render would produce: cut
    list, audio clips, subtitle cues/style/mode/track selection.
    """
    timeline = _get_timeline(db, timeline_id)
    timeline.content_version = (timeline.content_version or 0) + 1
    db.flush()
    return timeline.content_version


def update_timeline(
    db: Session,
    timeline_id: uuid.UUID,
    *,
    name: str | None = None,
    clip_order: list[dict[str, Any]] | None = None,
    subtitle_mode: str | None = None,
    subtitle_track_ids: list[str] | None = None,
) -> Any:
    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)  # 409 RENDER_LOCKED while rendering
    if name is not None:
        timeline.name = name.strip() or timeline.name
    if clip_order is not None:
        _set_clips(db, timeline.id, clip_order)
        bump_content_version(db, timeline.id)
    if subtitle_mode is not None:
        check_enum("subtitle_mode", subtitle_mode)
        if timeline.subtitle_mode != subtitle_mode:
            timeline.subtitle_mode = subtitle_mode
            bump_content_version(db, timeline.id)
    if subtitle_track_ids is not None:
        from app.models.subtitle import SubtitleTrack

        ids = [str(x) for x in subtitle_track_ids]
        for tid in ids:
            track = db.get(SubtitleTrack, uuid.UUID(tid))
            if track is None or track.project_id != timeline.project_id:
                raise StudioError(
                    ErrorCode.VALIDATION_ERROR,
                    f"Subtitle track not found in this project: {tid}.",
                )
        if (timeline.subtitle_track_ids or []) != ids:
            timeline.subtitle_track_ids = ids
            bump_content_version(db, timeline.id)
    # Design §4.15: single burns 1 track, dual burns 2.
    mode_now = timeline.subtitle_mode or "single"
    ids_now = timeline.subtitle_track_ids or []
    limit = {"single": 1, "dual": 2}.get(mode_now, 1)
    if len(ids_now) > limit:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"subtitle_mode={mode_now!r} accepts at most {limit} track(s), "
            f"got {len(ids_now)}.",
        )
    _mark_edited(timeline)
    db.flush()
    return timeline


def build_from_shots(db: Session, timeline_id: uuid.UUID) -> Any:
    """Auto-arrange: shots in order_index with their latest done clip (F-12)."""
    from app.models.production import Scene, Shot

    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)
    scenes = (
        db.query(Scene)
        .filter(Scene.project_id == timeline.project_id)
        .order_by(Scene.order_index)
        .all()
    )
    entries: list[dict[str, Any]] = []
    for scene in scenes:
        shots = (
            db.query(Shot)
            .filter(Shot.scene_id == scene.id)
            .order_by(Shot.order_index)
            .all()
        )
        for shot in shots:
            video = video_service.latest_done_video(db, shot.id)
            if video is None:
                continue
            entries.append(
                {
                    "shot_id": str(shot.id),
                    "video_id": str(video.id),
                    "in_sec": 0.0,
                    "out_sec": video.duration_sec,
                }
            )
    _set_clips(db, timeline.id, entries)
    bump_content_version(db, timeline.id)
    _mark_edited(timeline)
    db.flush()
    return timeline
