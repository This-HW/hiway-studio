"""TTS dialogue job creation (P1-5, design §8.1 / F-13).

POST /shots/{shot_id}/dialogue creates an `audio_generations` row
(kind='dialogue', status='queued') and dispatches it on q.audio.
The worker (tasks/tasks_audio.py) receives only the audio generation id;
the DB row is the source of truth (ARCHITECTURE §8).

Cost: TTS bills per_1k_chars (design §9.1) or per_char; the stub model is
free (cost_estimator supports both units).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.services import budget_guard, cost_estimator

#: File extensions accepted as a voice-clone sample without probing.
_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")


def get_audio_job(db: Session, audio_id: uuid.UUID):
    """Fetch one audio_generations row or raise NOT_FOUND (router read)."""
    from app.models.audio import AudioGeneration

    row = db.get(AudioGeneration, audio_id)
    if row is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Audio job not found.")
    return row


def clone_voice(
    db: Session, character_id: uuid.UUID, sample_asset_id: uuid.UUID
) -> str:
    """Clone a character voice from an uploaded audio sample (P1-5).

    Design §8.1: POST /characters/{id}/voice/clone {sample_asset_id} ->
    {voice_id}. The stub issues a deterministic voice id
    ("voice_<character-id-short>"); real adapters would upload the sample
    and return a provider voice id.
    """
    from app.models.asset import Asset
    from app.models.character import Character

    character = db.get(Character, character_id)
    if character is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Character not found.")
    asset = db.get(Asset, sample_asset_id)
    if asset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Sample asset not found.")
    if (
        character.project_id is not None
        and asset.project_id is not None
        and asset.project_id != character.project_id
    ):
        raise StudioError(
            ErrorCode.NOT_FOUND, "Sample asset not found in this project.",
            details={"asset_id": str(sample_asset_id)},
        )
    path_lower = str(asset.image_path or "").lower()
    if not path_lower.endswith(_AUDIO_EXTS):
        # Extension check is not authoritative; probe the file.
        from app.files import paths
        from app.media import ffmpeg_ops

        try:
            facts = ffmpeg_ops.probe(paths.projects_root() / asset.image_path)
        except Exception as exc:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "Sample asset is not a readable audio file.",
                details={"asset_id": str(sample_asset_id)},
            ) from exc
        if not facts.get("has_audio"):
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "Sample asset is not an audio file.",
                details={"asset_id": str(sample_asset_id)},
            )

    voice_id = f"voice_{character.id.hex[:8]}"
    character.voice_id = voice_id
    db.flush()
    return voice_id


def _get_shot(db: Session, shot_id: uuid.UUID):
    from app.models.production import Scene, Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Scene not found for shot.")
    return shot, scene.project_id


def _get_character(db: Session, character_id: uuid.UUID, project_id: uuid.UUID):
    from app.models.character import Character

    character = db.get(Character, character_id)
    if character is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Character not found.")
    # Global characters (project_id NULL) are reusable everywhere.
    if character.project_id is not None and character.project_id != project_id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Character belongs to a different project.",
            details={"character_id": str(character_id)},
        )
    return character


def _check_queue_full(db: Session) -> None:
    from app.models.audio import AudioGeneration

    pending = (
        db.query(AudioGeneration)
        .filter(AudioGeneration.status.in_(("queued", "running")))
        .count()
    )
    if pending > defaults.QUEUE_MAX_PENDING:
        raise StudioError(
            ErrorCode.QUEUE_FULL,
            "Job queue is full; try again later.",
            retryable=True,
            details={"retry_after_sec": defaults.QUEUE_FULL_RETRY_AFTER_SEC},
        )


def _resolve_tts_model(
    db: Session, project_id: uuid.UUID, explicit_model_id: str | None
) -> str:
    """explicit -> project default (tts) -> router rule (§3.3)."""
    from app.models.project import Project
    from app.models_lib import router

    project_default = None
    project = db.get(Project, project_id)
    if project is not None:
        project_default = (project.default_models or {}).get("tts")
    routed = router.route(
        task="tts",
        modality="tts",
        explicit_model_id=explicit_model_id,
        project_default_model_id=project_default,
        db=db,
    )
    return str(routed["model_id"])


def enqueue_dialogue(
    db: Session,
    shot_id: uuid.UUID,
    *,
    character_id: uuid.UUID,
    text: str,
    emotion_weights: dict[str, float] | None = None,
    model_id: str | None = None,
    target_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Create a dialogue TTS job. Returns {'job_id', 'estimated_cost_usd'}."""
    check_enum("job_type", "tts")
    shot, project_id = _get_shot(db, shot_id)
    character = _get_character(db, character_id, project_id)

    text = text.strip()
    if not text:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Dialogue text is empty.")

    _check_queue_full(db)
    resolved_model = _resolve_tts_model(db, project_id, model_id)
    estimated = cost_estimator.estimate(db, resolved_model, chars=len(text))
    budget_guard.check_budget(db, project_id, estimated_new=estimated)

    from app.db.session import dispatch_after_commit
    from app.models.audio import AudioGeneration

    params: dict[str, Any] = {}
    if target_duration_sec is not None:
        # The worker fits the synthesized audio to this length via
        # services.lipsync after synthesis (±10% rule, warn beyond).
        params["target_duration_sec"] = float(target_duration_sec)

    row = AudioGeneration(
        project_id=project_id,
        shot_id=shot.id,
        character_id=character.id,
        kind="dialogue",
        text=text,
        voice_id=character.voice_id,  # NULL until voice clone (P1-5)
        emotion_weights=dict(emotion_weights) if emotion_weights else None,
        model_id=resolved_model,
        params=params or None,
        status="queued",
        estimated_cost_usd=estimated,
    )
    db.add(row)
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.audio.run_generation", str(row.id))
    )
    return {"job_id": row.id, "estimated_cost_usd": str(estimated)}
