"""Export preset management (P0-18, ADR-0004).

Built-in presets (`project_id IS NULL`) are seeded idempotently and are
immutable through the API. Users may create project-scoped custom presets
and edit/delete only those.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "name": "YouTube 16:9",
        "platform": "youtube",
        "aspect_ratio": "16:9",
        "resolution": "1920x1080",
        "fps": 24,
        "codec": "h264",
        "audio_codec": "aac",
        "burn_subtitles": False,
    },
    {
        "name": "Shorts 9:16",
        "platform": "youtube_shorts",
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 24,
        "codec": "h264",
        "audio_codec": "aac",
        "burn_subtitles": False,
    },
    {
        "name": "Instagram Reels 9:16",
        "platform": "instagram_reels",
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 24,
        "codec": "h264",
        "audio_codec": "aac",
        "burn_subtitles": False,
    },
    {
        "name": "TikTok 9:16",
        "platform": "tiktok",
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 24,
        "codec": "h264",
        "audio_codec": "aac",
        "burn_subtitles": False,
    },
]


def ensure_builtin_presets(db: Session) -> int:
    """Upsert the built-in presets by name. Idempotent; returns rows touched."""
    from app.models.system import ExportPreset

    touched = 0
    for spec in BUILTIN_PRESETS:
        check_enum("aspect_ratio", spec["aspect_ratio"])
        row = (
            db.query(ExportPreset)
            .filter(ExportPreset.project_id.is_(None), ExportPreset.name == spec["name"])
            .first()
        )
        if row is None:
            db.add(ExportPreset(project_id=None, **spec))
        else:
            for key, value in spec.items():
                setattr(row, key, value)
        touched += 1
    db.flush()
    return touched


def list_presets(db: Session, project_id: uuid.UUID | None) -> list:
    """Built-ins plus the project's custom presets (built-ins first)."""
    from app.models.system import ExportPreset

    q = db.query(ExportPreset)
    if project_id is None:
        q = q.filter(ExportPreset.project_id.is_(None))
    else:
        q = q.filter(
            (ExportPreset.project_id.is_(None)) | (ExportPreset.project_id == project_id)
        )
    presets = q.all()
    presets.sort(key=lambda p: (p.project_id is not None, p.name))
    return presets


def _get_preset(db: Session, preset_id: uuid.UUID):
    from app.models.system import ExportPreset

    preset = db.get(ExportPreset, preset_id)
    if preset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Export preset not found.")
    return preset


def _require_custom(preset) -> None:
    if preset.project_id is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Built-in presets are read-only; duplicate one to customize.",
            details={"reason": "builtin_immutable", "preset_id": str(preset.id)},
        )


def create_custom_preset(
    db: Session,
    project_id: uuid.UUID,
    *,
    name: str,
    aspect_ratio: str,
    resolution: str,
    fps: int = 24,
    codec: str = "h264",
    audio_codec: str = "aac",
    burn_subtitles: bool = False,
):
    from app.models.project import Project
    from app.models.system import ExportPreset

    if db.get(Project, project_id) is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    check_enum("aspect_ratio", aspect_ratio)
    if not name.strip():
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Preset name is required.")
    preset = ExportPreset(
        project_id=project_id,
        name=name.strip(),
        platform="custom",
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        fps=fps,
        codec=codec,
        audio_codec=audio_codec,
        burn_subtitles=burn_subtitles,
    )
    db.add(preset)
    db.flush()
    return preset


def update_custom_preset(db: Session, preset_id: uuid.UUID, fields: dict[str, Any]):
    preset = _get_preset(db, preset_id)
    _require_custom(preset)
    allowed = {
        "name", "aspect_ratio", "resolution", "fps",
        "codec", "audio_codec", "burn_subtitles",
    }
    for key, value in fields.items():
        if key not in allowed:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR, f"Field is not editable: {key}."
            )
        if key == "aspect_ratio":
            check_enum("aspect_ratio", value)
        setattr(preset, key, value)
    db.flush()
    return preset


def delete_custom_preset(db: Session, preset_id: uuid.UUID) -> None:
    from app.models.timeline import Timeline

    preset = _get_preset(db, preset_id)
    _require_custom(preset)
    in_use = (
        db.query(Timeline).filter(Timeline.output_preset_id == preset.id).count()
    )
    if in_use:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Preset is used by timelines; reassign them first.",
            details={"reason": "preset_in_use", "timeline_count": in_use},
        )
    db.delete(preset)
    db.flush()
