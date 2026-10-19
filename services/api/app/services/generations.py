"""Shot image-generation job creation (F-06, F-07).

Thin routers call these; each function validates, assembles the prompt,
estimates cost, writes the generation rows (parent + children for batches,
D-25), and dispatches the Celery tasks. The task itself receives only the
generation id (ARCHITECTURE §8).

Queue-full guard (§12.3): more than `QUEUE_MAX_PENDING` queued jobs ->
429 QUEUE_FULL (retryable, retry_after_sec=60).
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
from app.core.sse_bridge import CHANNEL, get_bridge
from app.services import budget_guard


def _get_shot(db: Session, shot_id: uuid.UUID):
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    return shot


def _check_queue_full(db: Session) -> None:
    from app.models.generation import Generation

    pending = (
        db.query(Generation).filter(Generation.status == "queued").count()
    )
    if pending > defaults.QUEUE_MAX_PENDING:
        raise StudioError(
            ErrorCode.QUEUE_FULL,
            "Job queue is full; try again later.",
            retryable=True,
            details={"retry_after_sec": defaults.QUEUE_FULL_RETRY_AFTER_SEC},
        )


def _resolve_model_id(db: Session, shot, explicit_model_id: str | None) -> str:
    """explicit -> project default (image) -> router rule (§3.3)."""
    from app.models.production import Scene
    from app.models.project import Project
    from app.models_lib import router

    project_default = None
    scene = db.get(Scene, shot.scene_id)
    if scene is not None:
        project = db.get(Project, scene.project_id)
        if project is not None:
            project_default = (project.default_models or {}).get("image")
    routed = router.route(
        task="text_to_image",
        modality="image",
        explicit_model_id=explicit_model_id,
        project_default_model_id=project_default,
        db=db,
    )
    return str(routed["model_id"])


def _set_shot_generating(db: Session, shot) -> None:
    if shot.status in ("draft", "frame_done", "frame_approved", "failed"):
        check_enum("shot_status", "frame_generating")
        shot.status = "frame_generating"
    # A fresh generation resolves any stale-frame warning from prompt edits.
    shot.frame_stale = False
    db.flush()


def _dispatch(db: Session, child_id: uuid.UUID) -> None:
    """Put the generation on q.image after the rows commit (task arg = id only)."""
    from app.db.session import dispatch_after_commit

    dispatch_after_commit(db, lambda: dispatch_job("q.image.run_generation", str(child_id)))


def _new_generation(
    db: Session,
    *,
    project_id: uuid.UUID,
    shot_id: uuid.UUID,
    job_type: str,
    model_id: str,
    prompt_final: str,
    negative_final: str = "",
    reference_ids: list[str] | None = None,
    input_paths: list[str] | None = None,
    style_kit_id: uuid.UUID | None = None,
    style_kit_version: int | None = None,
    estimated_cost_usd: Decimal | None = None,
    is_batch: bool = False,
    parent_id: uuid.UUID | None = None,
    batch_index: int | None = None,
    seed: int | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    from app.models.generation import Generation

    check_enum("job_type", job_type)
    gen = Generation(
        project_id=project_id,
        shot_id=shot_id,
        job_type=job_type,
        model_id=model_id,
        prompt_final=prompt_final,
        negative_final=negative_final,
        reference_ids=reference_ids,
        input_paths=input_paths,
        style_kit_id=style_kit_id,
        style_kit_version=style_kit_version,
        estimated_cost_usd=estimated_cost_usd,
        is_batch=is_batch,
        parent_generation_id=parent_id,
        batch_index=batch_index,
        seed=seed,
        params=params or {},
        max_retries=defaults.RETRY_MAX_ATTEMPTS,
    )
    db.add(gen)
    db.flush()
    return gen


def _project_id_of(db: Session, shot) -> uuid.UUID:
    from app.models.production import Scene

    scene = db.get(Scene, shot.scene_id)
    return scene.project_id


def enqueue_frame(
    db: Session,
    shot_id: uuid.UUID,
    *,
    model_id: str | None = None,
    batch_size: int = 1,
    overrides: dict[str, str | None] | None = None,
    use_continuity: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """Create a frame-generation batch (parent + children, D-25)."""
    from app.services import continuity as continuity_service
    from app.services import cost_estimator
    from app.services import prompt_assembly

    if not 1 <= batch_size <= defaults.FRAME_BATCH_MAX:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"batch_size must be 1..{defaults.FRAME_BATCH_MAX}.",
        )
    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    # P1-3 seed resolution: an explicit seed pins shot.seed (fixed from now
    # on); otherwise the shot's pinned seed is reused; otherwise a fresh
    # random seed is drawn for this call (not pinned).
    if seed is not None:
        shot.seed = seed
    elif shot.seed is not None:
        seed = shot.seed
    else:
        seed = random.randrange(2**31)
    resolved_model = _resolve_model_id(db, shot, model_id)
    project_id = _project_id_of(db, shot)

    continuity_info: dict[str, Any] = {"available": False, "used": False}
    continuity_summary = continuity_image = None
    if use_continuity:
        src = continuity_service.get_continuity_source(db, shot_id)
        continuity_info = {
            "available": src["available"],
            "reason": src.get("reason", ""),
            "source_shot_id": src.get("source_shot_id"),
            "source_generation_id": src.get("source_generation_id"),
            "used": bool(src["available"]),
        }
        if src["available"]:
            continuity_summary = src["summary"]
            continuity_image = src["source_path"]

    assembled = prompt_assembly.assemble_shot_prompt(
        db,
        shot_id,
        overrides=overrides,
        continuity_summary=continuity_summary,
        continuity_image_path=continuity_image,
        continuity_source_id=(continuity_info.get("source_generation_id") or None),
        model_id=resolved_model,
    )
    per_image = cost_estimator.estimate_images(db, resolved_model, 1)
    total = cost_estimator.estimate_images(db, resolved_model, batch_size)
    budget_guard.check_budget(db, project_id, estimated_new=total)  # 403 BUDGET_EXCEEDED

    params_base: dict[str, Any] = {
        "truncated_sections": assembled["truncated_sections"],
        "continuity": continuity_info,
    }
    parent = _new_generation(
        db,
        project_id=project_id,
        shot_id=shot_id,
        job_type="text_to_image",
        model_id=resolved_model,
        prompt_final=assembled["prompt_final"],
        negative_final=assembled["negative_final"],
        reference_ids=assembled["reference_ids"],
        style_kit_id=assembled["style_kit_id"],
        style_kit_version=assembled["style_kit_version"],
        estimated_cost_usd=total,
        is_batch=True,
        params=params_base,
    )
    for i in range(batch_size):
        child_seed = seed + i
        child = _new_generation(
            db,
            project_id=project_id,
            shot_id=shot_id,
            job_type="text_to_image",
            model_id=resolved_model,
            prompt_final=assembled["prompt_final"],
            negative_final=assembled["negative_final"],
            reference_ids=assembled["reference_ids"],
            style_kit_id=assembled["style_kit_id"],
            style_kit_version=assembled["style_kit_version"],
            estimated_cost_usd=per_image,
            parent_id=parent.id,
            batch_index=i,
            seed=child_seed,
            params=dict(params_base),
        )
        _dispatch(db, child.id)

    _set_shot_generating(db, shot)
    db.flush()
    return {
        "job_id": parent.id,
        "estimated_cost_usd": total,
        "model_id": resolved_model,
        "continuity": continuity_info,
    }


def enqueue_retake(
    db: Session,
    shot_id: uuid.UUID,
    *,
    overrides: dict[str, str | None] | None = None,
    batch_size: int = 1,
    model_id: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Retake: a new batch with the same references.

    P1-3: an explicit seed pins shot.seed; otherwise the shot's pinned seed
    is reused; otherwise a fresh random seed is drawn.
    """
    return enqueue_frame(
        db, shot_id, model_id=model_id, batch_size=batch_size,
        overrides=overrides, seed=seed,
    )


def enqueue_variations(
    db: Session,
    shot_id: uuid.UUID,
    *,
    count: int = 3,
    variation_axis: str = "prompt",
    model_id: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """P1-3: create N variant batches for the same shot (D-25 each).

    variation_axis="prompt": N separate single-image batches, each with a
    marked prompt variant (real adapters see different prompts; the stub
    adapter only needs the seeds to differ).
    variation_axis="seed": one batch of N children with seeds base..base+N-1.
    """
    from app.services import cost_estimator
    from app.services import prompt_assembly

    if not 2 <= count <= 4:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "count must be 2..4."
        )
    if variation_axis not in ("prompt", "seed"):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "variation_axis must be 'prompt' or 'seed'.",
        )
    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    # Same seed semantics as enqueue_frame (explicit pins, else pinned, else random).
    base_seed = seed if seed is not None else shot.seed
    if base_seed is None:
        base_seed = random.randrange(2**31)
    if seed is not None:
        shot.seed = seed
    resolved_model = _resolve_model_id(db, shot, model_id)
    project_id = _project_id_of(db, shot)

    assembled = prompt_assembly.assemble_shot_prompt(
        db,
        shot_id,
        model_id=resolved_model,
    )
    per_image = cost_estimator.estimate_images(db, resolved_model, 1)
    total = cost_estimator.estimate_images(db, resolved_model, count)
    budget_guard.check_budget(db, project_id, estimated_new=total)  # 403 BUDGET_EXCEEDED

    params_base: dict[str, Any] = {
        "truncated_sections": assembled["truncated_sections"],
        "continuity": {"available": False, "used": False},
        "variation_axis": variation_axis,
    }
    parents: list[Any] = []
    variations: list[dict[str, Any]] = []
    if variation_axis == "seed":
        # One parent batch, N children with consecutive seeds.
        parent = _new_generation(
            db,
            project_id=project_id,
            shot_id=shot_id,
            job_type="text_to_image",
            model_id=resolved_model,
            prompt_final=assembled["prompt_final"],
            negative_final=assembled["negative_final"],
            reference_ids=assembled["reference_ids"],
            style_kit_id=assembled["style_kit_id"],
            style_kit_version=assembled["style_kit_version"],
            estimated_cost_usd=total,
            is_batch=True,
            params=dict(params_base),
        )
        parents.append(parent)
        for i in range(count):
            child_seed = base_seed + i
            child = _new_generation(
                db,
                project_id=project_id,
                shot_id=shot_id,
                job_type="text_to_image",
                model_id=resolved_model,
                prompt_final=assembled["prompt_final"],
                negative_final=assembled["negative_final"],
                reference_ids=assembled["reference_ids"],
                style_kit_id=assembled["style_kit_id"],
                style_kit_version=assembled["style_kit_version"],
                estimated_cost_usd=per_image,
                parent_id=parent.id,
                batch_index=i,
                seed=child_seed,
                params={**params_base, "variation_index": i},
            )
            _dispatch(db, child.id)
            variations.append(
                {"job_id": parent.id, "prompt_final": child.prompt_final, "seed": child_seed}
            )
    else:
        # N single-image batches with marked prompt variants.
        for i in range(count):
            child_seed = base_seed + i
            prompt_final = (
                f"{assembled['prompt_final']}\n\n"
                f"[variation {i + 1} of {count}: alternate interpretation]"
            )
            parent = _new_generation(
                db,
                project_id=project_id,
                shot_id=shot_id,
                job_type="text_to_image",
                model_id=resolved_model,
                prompt_final=prompt_final,
                negative_final=assembled["negative_final"],
                reference_ids=assembled["reference_ids"],
                style_kit_id=assembled["style_kit_id"],
                style_kit_version=assembled["style_kit_version"],
                estimated_cost_usd=per_image,
                is_batch=True,
                params={**params_base, "variation_index": i},
            )
            child = _new_generation(
                db,
                project_id=project_id,
                shot_id=shot_id,
                job_type="text_to_image",
                model_id=resolved_model,
                prompt_final=prompt_final,
                negative_final=assembled["negative_final"],
                reference_ids=assembled["reference_ids"],
                style_kit_id=assembled["style_kit_id"],
                style_kit_version=assembled["style_kit_version"],
                estimated_cost_usd=per_image,
                parent_id=parent.id,
                batch_index=0,
                seed=child_seed,
                params={**params_base, "variation_index": i},
            )
            _dispatch(db, child.id)
            parents.append(parent)
            variations.append(
                {"job_id": parent.id, "prompt_final": prompt_final, "seed": child_seed}
            )

    _set_shot_generating(db, shot)
    db.flush()
    return {
        "job_id": parents[0].id,
        "variation_job_ids": [p.id for p in parents],
        "variations": variations,
        "count": count,
        "variation_axis": variation_axis,
        "model_id": resolved_model,
        "estimated_cost_usd": total,
    }


def _source_take(db: Session, shot, generation_id: uuid.UUID):
    from app.models.generation import Generation

    gen = db.get(Generation, generation_id)
    if gen is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Source generation not found.")
    if gen.shot_id != shot.id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "Source generation does not belong to this shot."
        )
    if gen.is_batch:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Use a batch child (take), not the batch parent.",
            details={"reason": "parent_not_child"},
        )
    if gen.status != "succeeded" or not gen.output_path:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Source take must be a succeeded generation with a saved image.",
            details={"status": gen.status},
        )
    # ADR-0011 §10: multipass (upscale/outpaint/inpaint) only on approved takes.
    if str(gen.id) != str(shot.approved_frame_generation_id or ""):
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            "Multipass post-processing requires the approved take. Approve this take first.",
            details={"generation_id": str(gen.id)},
        )
    return gen


def _resolve_rel_path(rel_path: str) -> str:
    """Validate a project-relative path (no traversal, must exist)."""
    from app.files import paths

    root = paths.projects_root()
    abs_path = (root / rel_path).resolve()
    try:
        abs_path.relative_to(root.resolve())
    except ValueError:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Path escapes the projects root.")
    if not abs_path.is_file():
        raise StudioError(
            ErrorCode.NOT_FOUND, "Referenced file not found.", details={"path": rel_path}
        )
    return rel_path


def enqueue_inpaint(
    db: Session,
    shot_id: uuid.UUID,
    *,
    generation_id: uuid.UUID,
    mask_path: str,
    prompt: str,
    model_id: str | None = None,
) -> dict[str, Any]:
    from app.services import cost_estimator

    if not prompt or not prompt.strip():
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Inpaint prompt must not be empty.")
    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    src = _source_take(db, shot, generation_id)
    mask_rel = _resolve_rel_path(mask_path)
    resolved_model = _resolve_model_id(db, shot, model_id)
    cost = cost_estimator.estimate_images(db, resolved_model, 1)
    budget_guard.check_budget(db, _project_id_of(db, shot), estimated_new=cost)  # 403 BUDGET_EXCEEDED

    gen = _new_generation(
        db,
        project_id=_project_id_of(db, shot),
        shot_id=shot_id,
        job_type="inpaint",
        model_id=resolved_model,
        prompt_final=prompt.strip(),
        reference_ids=[str(src.id)],
        input_paths=[src.output_path, mask_rel],
        estimated_cost_usd=cost,
        params={"source_generation_id": str(src.id), "mask_path": mask_rel},
    )
    _set_shot_generating(db, shot)
    _dispatch(db, gen.id)
    db.flush()
    return {"job_id": gen.id, "estimated_cost_usd": cost, "model_id": resolved_model}


def enqueue_upscale(
    db: Session, shot_id: uuid.UUID, *, generation_id: uuid.UUID, model_id: str | None = None
) -> dict[str, Any]:
    from app.services import cost_estimator

    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    src = _source_take(db, shot, generation_id)
    resolved_model = _resolve_model_id(db, shot, model_id)
    cost = cost_estimator.estimate_images(db, resolved_model, 1)
    budget_guard.check_budget(db, _project_id_of(db, shot), estimated_new=cost)  # 403 BUDGET_EXCEEDED

    gen = _new_generation(
        db,
        project_id=_project_id_of(db, shot),
        shot_id=shot_id,
        job_type="upscale",
        model_id=resolved_model,
        prompt_final=f"Upscale: {src.prompt_final or ''}".strip(),
        reference_ids=[str(src.id)],
        input_paths=[src.output_path],
        estimated_cost_usd=cost,
        params={"source_generation_id": str(src.id)},
    )
    _set_shot_generating(db, shot)
    _dispatch(db, gen.id)
    db.flush()
    return {"job_id": gen.id, "estimated_cost_usd": cost, "model_id": resolved_model}


def enqueue_outpaint(
    db: Session,
    shot_id: uuid.UUID,
    *,
    generation_id: uuid.UUID,
    prompt: str,
    mask_path: str | None = None,
    direction: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    from app.services import cost_estimator

    if not prompt or not prompt.strip():
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Outpaint prompt must not be empty.")
    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    src = _source_take(db, shot, generation_id)
    mask_rel = _resolve_rel_path(mask_path) if mask_path else None
    resolved_model = _resolve_model_id(db, shot, model_id)
    cost = cost_estimator.estimate_images(db, resolved_model, 1)
    budget_guard.check_budget(db, _project_id_of(db, shot), estimated_new=cost)  # 403 BUDGET_EXCEEDED

    inputs = [src.output_path] + ([mask_rel] if mask_rel else [])
    gen = _new_generation(
        db,
        project_id=_project_id_of(db, shot),
        shot_id=shot_id,
        job_type="outpaint",
        model_id=resolved_model,
        prompt_final=prompt.strip(),
        reference_ids=[str(src.id)],
        input_paths=inputs,
        estimated_cost_usd=cost,
        params={
            "source_generation_id": str(src.id),
            "mask_path": mask_rel,
            "direction": direction,
        },
    )
    _set_shot_generating(db, shot)
    _dispatch(db, gen.id)
    db.flush()
    return {"job_id": gen.id, "estimated_cost_usd": cost, "model_id": resolved_model}


def _terminal(status: str) -> bool:
    return status in ("succeeded", "failed", "canceled")


def retry_job(db: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Re-queue a failed generation (or a batch parent's failed children).

    Raises 404 when the job is missing, 400 VALIDATION_ERROR when nothing is
    retryable.
    """
    from app.models.generation import Generation

    job = db.get(Generation, job_id)
    if job is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Job not found.")
    targets = (
        db.query(Generation)
        .filter(Generation.parent_generation_id == job.id, Generation.status == "failed")
        .all()
        if job.is_batch
        else ([job] if job.status == "failed" else [])
    )
    if not targets:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Only failed jobs can be retried.",
            details={"status": job.status},
        )
    for target in targets:
        target.status = "queued"
        target.progress_pct = 0.0
        target.started_at = None
        target.finished_at = None
        target.error_code = None
        target.error_message = None
        _dispatch(db, target.id)
    if job.is_batch:
        job.status = "queued"
        job.finished_at = None
    db.flush()
    return {"job_id": job.id, "estimated_cost_usd": job.estimated_cost_usd}


def cancel_job(db: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Cancel a queued/running generation (and queued/running batch children)."""
    from datetime import datetime, timezone

    from app.models.generation import Generation

    job = db.get(Generation, job_id)
    if job is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Job not found.")
    targets = (
        db.query(Generation)
        .filter(Generation.parent_generation_id == job.id)
        .all()
        if job.is_batch
        else [job]
    )
    canceled_any = False
    now = datetime.now(timezone.utc)
    for target in targets:
        if not _terminal(target.status):
            target.status = "canceled"
            target.finished_at = now
            target.error_code = "canceled"
            target.error_message = "Canceled by user."
            canceled_any = True
    if not canceled_any:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Only queued or running jobs can be canceled.",
            details={"status": job.status},
        )
    if job.is_batch and all(_terminal(t.status) for t in targets):
        job.status = "canceled"
        job.finished_at = now
    db.flush()
    get_bridge().publish(
        CHANNEL,
        "job.canceled",
        {"job_id": str(job.id), "job_type": job.job_type},
    )
    return {"job_id": job.id}


def list_jobs(
    db: Session,
    *,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    job_type: str | None = None,
    shot_id: uuid.UUID | None = None,
    model_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Any]:
    """Filtered job list, newest first (P0-14)."""
    from app.models.generation import Generation

    query = db.query(Generation)
    if project_id:
        query = query.filter(Generation.project_id == project_id)
    if status:
        query = query.filter(Generation.status == status)
    if job_type:
        query = query.filter(Generation.job_type == job_type)
    if shot_id:
        query = query.filter(Generation.shot_id == shot_id)
    if model_id:
        query = query.filter(Generation.model_id == model_id)
    return (
        query.order_by(Generation.created_at.desc()).limit(limit).offset(offset).all()
    )


def get_job_with_children(db: Session, job_id: uuid.UUID) -> tuple[Any, list[Any] | None]:
    """Return (job, children) — children is None for non-batch jobs."""
    from app.models.generation import Generation

    job = db.get(Generation, job_id)
    if job is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Job not found.")
    children = None
    if job.is_batch:
        children = (
            db.query(Generation)
            .filter(Generation.parent_generation_id == job.id)
            .order_by(Generation.batch_index)
            .all()
        )
    return job, children
