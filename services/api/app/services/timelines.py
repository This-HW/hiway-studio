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

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.services import videos as video_service
from app.services.render import _require_editable, render_locked


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


def _mark_edited(db: Session, timeline) -> None:
    """A content edit returns a finished timeline to draft (re-render required).

    Conditional UPDATE against the CURRENT row (B4B-H4): only `done` becomes
    `draft`; a cached status is never written back, so a render claim that
    committed after this session read the timeline is never overwritten."""
    from app.models.timeline import Timeline

    check_enum("render_status", "draft")
    db.flush()
    db.execute(
        update(Timeline)
        .where(Timeline.id == timeline.id, Timeline.status == "done")
        .values(status="draft")
        .execution_options(synchronize_session=False)
    )
    db.expire(timeline, ["status"])


def _lock_timeline(db: Session, timeline_id: uuid.UUID):
    """Row-lock the timeline (SELECT ... FOR NO KEY UPDATE; SQLite serializes
    writers) and reload it; 409 RENDER_LOCKED when it is rendering now.

    THE lock order of every timeline edit (B4BR2-M1): Project (only when the
    subtitle selection / mode changes, `lock_subtitle_scope`) -> Timeline
    (here) -> clip / content rows. Taken before any `timeline_clips` write,
    so a clip-only edit and a rename + clip edit can never hold the clip
    rows and the Timeline row in opposite order (Postgres 40P01)."""
    from app.models.timeline import Timeline
    from app.services.generations import lock_row

    timeline = lock_row(db, Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    _require_editable(timeline)
    return timeline


def _rename(db: Session, timeline, name: str) -> None:
    """Metadata-only write: conditional on not rendering, never touches status."""
    from app.models.timeline import Timeline

    result = db.execute(
        update(Timeline)
        .where(Timeline.id == timeline.id, Timeline.status != "rendering")
        .values(name=name)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise render_locked(timeline.id)
    db.expire(timeline, ["name", "status"])


def bump_content_version(db: Session, timeline_id: uuid.UUID) -> int:
    """Bump the render-content version (design §10.4 staleness gate).

    Call after every edit that changes what a render would produce: cut
    list, audio clips, subtitle cues/style/mode/track selection.

    Also the render lock (F13): the bump is a conditional UPDATE that only
    matches a timeline that is not rendering, so it serializes with
    request_render's claim — an edit racing a render claim either lands
    before it (the claim then sees a new content_version and fails as stale)
    or raises 409 RENDER_LOCKED.
    """
    from app.models.timeline import Timeline

    timeline = _get_timeline(db, timeline_id)
    db.flush()  # pending ORM edits first; the UPDATE below must not be lost
    result = db.execute(
        update(Timeline)
        .where(Timeline.id == timeline.id, Timeline.status != "rendering")
        .values(content_version=func.coalesce(Timeline.content_version, 0) + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise render_locked(timeline.id)
    db.expire(timeline, ["content_version", "status"])
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
    """Edit a timeline. Every write is guarded against a concurrent render
    claim by the CURRENT row state (B4B-H4): content edits through the
    conditional `bump_content_version`, the name through a conditional
    UPDATE, and done -> draft only through `_mark_edited`'s conditional
    UPDATE — a status read earlier by this session is never written back.
    Track selection / subtitle mode changes first take the subtitle scope
    lock shared with cue/style/track edits (B4B-H3). Lock order (B4BR2-M1):
    Project (selection / mode only) -> Timeline -> clip rows, the Timeline
    row lock taken before ANY write of this edit."""
    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)  # 409 RENDER_LOCKED while rendering (fast path)
    if subtitle_mode is not None or subtitle_track_ids is not None:
        from app.services.subtitles import lock_subtitle_scope

        # First lock of this transaction (order project -> timeline).
        lock_subtitle_scope(db, timeline.project_id)
    # Timeline row lock before any clip/content write; reloads the row (the
    # selection / status as committed now, under the lock).
    timeline = _lock_timeline(db, timeline.id)
    content_changed = False
    if name is not None and name.strip():
        _rename(db, timeline, name.strip())
    if clip_order is not None:
        _set_clips(db, timeline.id, clip_order)
        bump_content_version(db, timeline.id)
        content_changed = True
    if subtitle_mode is not None:
        check_enum("subtitle_mode", subtitle_mode)
        if timeline.subtitle_mode != subtitle_mode:
            timeline.subtitle_mode = subtitle_mode
            bump_content_version(db, timeline.id)
            content_changed = True
    if subtitle_track_ids is not None:
        from app.models.subtitle import SubtitleTrack

        ids = [str(x) for x in subtitle_track_ids]
        for tid in ids:
            track = db.get(SubtitleTrack, uuid.UUID(tid), populate_existing=True)
            if track is None or track.project_id != timeline.project_id:
                raise StudioError(
                    ErrorCode.VALIDATION_ERROR,
                    f"Subtitle track not found in this project: {tid}.",
                )
        if (timeline.subtitle_track_ids or []) != ids:
            timeline.subtitle_track_ids = ids
            bump_content_version(db, timeline.id)
            content_changed = True
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
    if content_changed:
        _mark_edited(db, timeline)
    db.flush()
    return timeline


def build_from_shots(db: Session, timeline_id: uuid.UUID) -> Any:
    """Auto-arrange: shots in order_index with their latest done clip (F-12)."""
    from app.models.production import Scene, Shot

    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)  # fast path
    timeline = _lock_timeline(db, timeline.id)  # B4BR2-M1: before clip writes
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
    _mark_edited(db, timeline)
    db.flush()
    return timeline
