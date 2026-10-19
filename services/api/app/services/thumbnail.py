"""Thumbnail generator (P1-9, design §8.1, §10.5).

Flow:
  1. POST /projects/{id}/thumbnails -> request_thumbnails(): validates count
     (2-8, default 4) and the optional timeline, checks the budget
     (per_image x count), creates a batch parent + N child generations
     (job_type='thumbnail', D-25), and dispatches the children on q.image.
  2. The image worker saves each child under projects/{id}/thumbnails/ and
     composites the title text as a post-processing overlay (PIL) — the image
     model is explicitly told NOT to render text (§10.5: text from image
     models is unreliable, so overlay is the only text path).

The overlay itself lives in app/media/thumbnail_text.py (media may use files;
services may use media). tasks_image calls it via the "_thumbnail" params key.
"""
from __future__ import annotations

import random
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.db.session import dispatch_after_commit
from app.services import budget_guard, cost_estimator

JOB_TYPE = "thumbnail"

MIN_COUNT = 2
MAX_COUNT = 8
DEFAULT_COUNT = 4


def _get_project(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


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


def _resolve_image_model(db: Session, project, explicit_model_id: str | None = None) -> str:
    from app.models_lib import router

    routed = router.route(
        task="text_to_image",
        modality="image",
        explicit_model_id=explicit_model_id,
        project_default_model_id=(project.default_models or {}).get("image"),
        db=db,
    )
    return str(routed["model_id"])


def _thumbnail_prompt(project, timeline, title_text: str | None, index: int) -> str:
    subject = title_text or (timeline.name if timeline else None) or project.name
    return (
        f"Cinematic video thumbnail artwork about: {subject}. "
        f"Bold composition, high contrast, vibrant colors, dramatic lighting, "
        f"16:9 wide framing, variation {index + 1}. "
        f"IMPORTANT: no text, no letters, no words, no watermark in the image."
    )


def request_thumbnails(
    db: Session,
    project_id: uuid.UUID,
    *,
    timeline_id: str | None = None,
    title_text: str | None = None,
    count: int = DEFAULT_COUNT,
) -> dict[str, Any]:
    """Create the thumbnail batch (parent + children) and queue the children."""
    from app.models.generation import Generation
    from app.models.timeline import Timeline

    if not (MIN_COUNT <= count <= MAX_COUNT):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"count must be {MIN_COUNT}..{MAX_COUNT}.",
        )
    project = _get_project(db, project_id)
    timeline = None
    if timeline_id is not None:
        timeline = db.get(Timeline, uuid.UUID(timeline_id))
        if timeline is None or timeline.project_id != project.id:
            raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found in this project.")
    _check_queue_full(db)

    check_enum("job_type", JOB_TYPE)
    model_id = _resolve_image_model(db, project)
    per_image = cost_estimator.estimate_images(db, model_id, 1)
    total = per_image * count
    budget_guard.check_budget(db, project.id, estimated_new=total)

    parent = Generation(
        project_id=project.id,
        shot_id=None,
        job_type=JOB_TYPE,
        model_id=model_id,
        estimated_cost_usd=total,
        is_batch=True,
        params={
            "thumbnail_batch": True,
            "title_text": title_text,
            "timeline_id": timeline_id,
            "count": count,
        },
        max_retries=defaults.RETRY_MAX_ATTEMPTS,
    )
    db.add(parent)
    db.flush()

    job_ids: list[str] = []
    for i in range(count):
        child = Generation(
            project_id=project.id,
            shot_id=None,
            job_type=JOB_TYPE,
            model_id=model_id,
            prompt_final=_thumbnail_prompt(project, timeline, title_text, i),
            negative_final="text, letters, words, watermark, blurry, low quality",
            estimated_cost_usd=per_image,
            is_batch=False,
            parent_generation_id=parent.id,
            batch_index=i,
            seed=random.randint(0, 2**31 - 1),
            params={"_thumbnail": {"title_text": title_text, "index": i}},
            max_retries=defaults.RETRY_MAX_ATTEMPTS,
        )
        db.add(child)
        db.flush()
        job_ids.append(str(child.id))
        dispatch_after_commit(
            db, lambda gid=str(child.id): dispatch_job("q.image.run_generation", gid)
        )
    db.flush()
    return {"job_id": parent.id, "estimated_cost_usd": total, "count": count,
            "child_job_ids": job_ids}
