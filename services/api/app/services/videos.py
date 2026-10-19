"""Shot video generation (I2V/T2V, F-10, P0-13).

Thin routers call `enqueue_video`; the Celery worker (tasks_video) owns the
execution. Task signature is fixed: run_generation(generation_id) — id only.

Motion-prompt rule (§5.9): the prompt must describe ONLY what should move or
change — never re-describe what is already in the image. The service enforces
presence and length; the semantic rule is surfaced in the UI placeholder and
API docs because it cannot be validated mechanically.

I2V requires an approved frame (409 APPROVAL_REQUIRED via
services.approval.require_approved_frame). T2V is allowed without approval;
the UI warns when the shot has characters (consistency risk) but the server
does not block (design F-10).

Resolution default: the project aspect_ratio maps to a clip resolution
(worker overwrites it with the probed actuals after the run).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.services import approval, budget_guard, cost_estimator

# Requested clip resolution per project aspect ratio. The worker replaces
# these with the provider's actual output dimensions after probing.
_ASPECT_RESOLUTION = {
    "16:9": "1920x1080",
    "9:16": "1080x1920",
    "1:1": "1080x1080",
    "2.39:1": "1920x804",
    "4:5": "1080x1350",
}


def _get_shot(db: Session, shot_id: uuid.UUID):
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    return shot


def _check_queue_full(db: Session) -> None:
    from app.models.generation import Generation

    pending = db.query(Generation).filter(Generation.status == "queued").count()
    if pending > defaults.QUEUE_MAX_PENDING:
        raise StudioError(
            ErrorCode.QUEUE_FULL,
            "Job queue is full; try again later.",
            retryable=True,
            details={"retry_after_sec": defaults.QUEUE_FULL_RETRY_AFTER_SEC},
        )


def _resolve_model_id(
    db: Session, shot, explicit_model_id: str | None, *, job_type: str
) -> str:
    from app.models.production import Scene
    from app.models.project import Project
    from app.models_lib import router

    project_default = None
    scene = db.get(Scene, shot.scene_id)
    if scene is not None:
        project = db.get(Project, scene.project_id)
        if project is not None:
            project_default = (project.default_models or {}).get("video")
    routed = router.route(
        task=job_type,
        modality="video",
        explicit_model_id=explicit_model_id,
        project_default_model_id=project_default,
        db=db,
    )
    return str(routed["model_id"])


def _check_motion_prompt(motion_prompt: str | None) -> str:
    if not motion_prompt or not motion_prompt.strip():
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "motion_prompt is required: describe what should move or change.",
        )
    text = motion_prompt.strip()
    if len(text) > defaults.PROMPT_MAX_CHARS:
        raise StudioError(
            ErrorCode.PROMPT_TOO_LONG,
            f"motion_prompt exceeds {defaults.PROMPT_MAX_CHARS} chars.",
            details={"max_chars": defaults.PROMPT_MAX_CHARS, "actual_chars": len(text)},
        )
    return text


def _clamp_duration(db: Session, model_id: str, requested: float | None, fallback: float) -> float:
    from app.models_lib import registry

    duration = requested if requested is not None else fallback
    if duration <= 0:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "duration_sec must be > 0.")
    model = registry.get_model(model_id, db)
    max_dur = (model.get("constraints") or {}).get("max_duration_sec")
    if max_dur is not None:
        duration = min(duration, float(max_dur))  # N-01: dynamic limit
    return duration


def enqueue_video(
    db: Session,
    shot_id: uuid.UUID,
    *,
    mode: str,
    motion_prompt: str | None,
    duration_sec: float | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Validate, create the generation + video rows, dispatch q.video.

    Returns {"job_id", "video_id", "estimated_cost_usd"}.
    """
    from app.db.session import dispatch_after_commit
    from app.models.generation import Generation, Video
    from app.models.production import Scene, Shot
    from app.models.project import Project

    check_enum("video_mode", mode)
    shot = _get_shot(db, shot_id)
    prompt = _check_motion_prompt(motion_prompt)

    approved_gen = None
    input_paths: list[str] | None = None
    if mode == "i2v":
        approved_gen = approval.require_approved_frame(db, shot)  # 409 when unapproved
        input_paths = [approved_gen.output_path]

    job_type = "image_to_video" if mode == "i2v" else "text_to_video"
    check_enum("job_type", job_type)
    resolved_model_id = _resolve_model_id(db, shot, model_id, job_type=job_type)
    duration = _clamp_duration(db, resolved_model_id, duration_sec, shot.duration_sec or 5.0)
    _check_queue_full(db)
    estimated = cost_estimator.estimate(db, resolved_model_id, duration_sec=duration)

    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Scene not found for shot.")
    budget_guard.check_budget(db, scene.project_id, estimated_new=estimated)  # 403 BUDGET_EXCEEDED
    project = db.get(Project, scene.project_id)
    aspect = project.aspect_ratio if project else "16:9"
    resolution = _ASPECT_RESOLUTION.get(aspect, "1920x1080")

    gen = Generation(
        project_id=scene.project_id,
        shot_id=shot.id,
        job_type=job_type,
        model_id=resolved_model_id,
        prompt_final=prompt,
        input_paths=input_paths,
        params={"mode": mode, "motion_prompt": prompt, "duration_sec": duration},
        estimated_cost_usd=estimated,
    )
    db.add(gen)
    db.flush()

    video = Video(
        shot_id=shot.id,
        generation_id=gen.id,
        mode=mode,
        motion_prompt=prompt,
        duration_sec=duration,
        fps=defaults.OUTPUT_FPS,
        resolution=resolution,
        model_id=resolved_model_id,
        output_path="",  # filled by the worker after the file is saved
        status="generating",
    )
    db.add(video)
    db.flush()

    check_enum("shot_status", "video_generating")
    shot.status = "video_generating"
    db.flush()

    dispatch_after_commit(db, lambda: dispatch_job("q.video.run_generation", str(gen.id)))
    return {
        "job_id": gen.id,
        "video_id": video.id,
        "estimated_cost_usd": estimated,
    }


def list_videos(db: Session, shot_id: uuid.UUID) -> list:
    """Newest-first clip history for a shot."""
    from app.models.generation import Video

    _get_shot(db, shot_id)
    return (
        db.query(Video)
        .filter(Video.shot_id == shot_id)
        .order_by(Video.created_at.desc())
        .all()
    )


def latest_done_video(db: Session, shot_id: uuid.UUID):
    """Newest done clip of a shot (used by build-from-shots)."""
    from app.models.generation import Video

    return (
        db.query(Video)
        .filter(Video.shot_id == shot_id, Video.status == "done")
        .order_by(Video.created_at.desc())
        .first()
    )
