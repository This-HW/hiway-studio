"""Audio upload registration + timeline placement (P0 audio scope, design §4.14).

P0 has no audio *generation* (TTS/voice-clone/dubbing are P1-5): uploads
arrive through `POST /assets/upload` and are registered here as
`audio_generations` rows (`status='done'`, kind bgm|sfx), then placed on a
timeline as `timeline_audio_clips`. The render worker mixes them under the
video (tasks_post -> services/render).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.services.render import _require_editable


def _get_timeline(db: Session, timeline_id: uuid.UUID):
    from app.models.timeline import Timeline

    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    return timeline


def register_from_asset(
    db: Session, asset_id: uuid.UUID, *, kind: str = "bgm",
    project_id: uuid.UUID | None = None,
) -> Any:
    """Wrap an uploaded audio asset as an audio_generations row.

    `project_id` (the path project) must own the asset; global-library
    assets (project_id NULL) are reusable across projects.
    """
    from app.models.asset import Asset
    from app.models.audio import AudioGeneration
    from app.files import paths
    from app.media import ffmpeg_ops

    check_enum("audio_kind", kind)
    if kind not in ("bgm", "sfx"):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "P0 audio upload supports kind bgm|sfx only (TTS/dubbing are P1).",
            details={"kind": kind},
        )
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Asset not found.")
    if (
        project_id is not None
        and asset.project_id is not None
        and asset.project_id != project_id
    ):
        raise StudioError(
            ErrorCode.NOT_FOUND, "Asset not found in this project.",
            details={"asset_id": str(asset_id)},
        )
    abs_path = paths.projects_root() / asset.image_path
    try:
        facts = ffmpeg_ops.probe(abs_path)
    except Exception as exc:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Asset is not a readable audio file.",
            details={"asset_id": str(asset_id)},
        ) from exc
    if not facts.get("has_audio") and not str(asset.image_path).lower().endswith(
        (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")
    ):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Asset does not look like an audio file.",
            details={"asset_id": str(asset_id)},
        )
    row = AudioGeneration(
        project_id=asset.project_id,
        kind=kind,
        output_path=asset.image_path,  # relative path, shared with the asset
        duration_sec=facts.get("duration_sec") or None,
        status="done",
    )
    db.add(row)
    db.flush()
    return row


def list_audio(
    db: Session, project_id: uuid.UUID, *, kind: str | None = None
) -> list:
    from app.models.audio import AudioGeneration

    q = db.query(AudioGeneration).filter(AudioGeneration.project_id == project_id)
    if kind is not None:
        check_enum("audio_kind", kind)
        q = q.filter(AudioGeneration.kind == kind)
    return q.order_by(AudioGeneration.created_at.desc()).all()


def list_clips(db: Session, timeline_id: uuid.UUID) -> list:
    from app.models.audio import TimelineAudioClip

    _get_timeline(db, timeline_id)
    return (
        db.query(TimelineAudioClip)
        .filter(TimelineAudioClip.timeline_id == timeline_id)
        .order_by(TimelineAudioClip.start_sec)
        .all()
    )


def place_clip(
    db: Session,
    timeline_id: uuid.UUID,
    *,
    audio_id: uuid.UUID,
    start_sec: float,
    duration_sec: float | None = None,
    gain_db: float = 0.0,
) -> Any:
    from app.models.audio import AudioGeneration, TimelineAudioClip
    from app.services.timelines import bump_content_version

    timeline = _get_timeline(db, timeline_id)
    _require_editable(timeline)
    audio = db.get(AudioGeneration, audio_id)
    if audio is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Audio not found.")
    if audio.project_id != timeline.project_id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "Audio belongs to a different project."
        )
    if audio.status != "done" or not audio.output_path:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "Audio is not ready for placement."
        )
    start = float(start_sec)
    if start < 0:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "start_sec must be >= 0.")
    if duration_sec is not None and float(duration_sec) <= 0:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "duration_sec must be > 0.")
    check_enum("audio_kind", audio.kind)
    clip = TimelineAudioClip(
        timeline_id=timeline_id,
        audio_id=audio.id,
        kind=audio.kind,
        start_sec=start,
        duration_sec=float(duration_sec) if duration_sec is not None else None,
        gain_db=float(gain_db),
    )
    db.add(clip)
    bump_content_version(db, timeline_id)
    db.flush()
    return clip


def delete_clip(db: Session, clip_id: uuid.UUID) -> None:
    from app.models.audio import TimelineAudioClip
    from app.services.timelines import bump_content_version

    clip = db.get(TimelineAudioClip, clip_id)
    if clip is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Audio clip not found.")
    timeline = _get_timeline(db, clip.timeline_id)
    _require_editable(timeline)
    bump_content_version(db, timeline.id)
    db.delete(clip)
    db.flush()
