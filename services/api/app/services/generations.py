"""Shot image-generation job creation (F-06, F-07).

Thin routers call these; each function validates, assembles the prompt,
estimates cost, writes the generation rows (parent + children for batches,
D-25), and dispatches the Celery tasks. The task itself receives only the
generation id (ARCHITECTURE §8).

Queue-full guard (§12.3): more than `QUEUE_MAX_PENDING` queued jobs ->
429 QUEUE_FULL (retryable, retry_after_sec=60).
"""
from __future__ import annotations

import copy
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
from app.services import budget_guard, provider_input


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


def _resolve_model_id(
    db: Session, shot, explicit_model_id: str | None, task: str = "text_to_image"
) -> str:
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
        task=task,
        modality="image",
        explicit_model_id=explicit_model_id,
        project_default_model_id=project_default,
        db=db,
    )
    return str(routed["model_id"])


# Shot columns that feed the frame prompt (prompt_assembly) or count as a
# prompt edit (scene.update_shot marks frame_stale when one changes, F-06).
FRAME_PROMPT_FIELDS = (
    "scene_id", "shot_type", "camera_angle", "movement", "action", "dialogue",
    "emotion_weights", "style_kit_id", "camera_preset_id", "camera_side",
)


def frame_prompt_inputs(shot) -> dict[str, Any]:
    """Snapshot of the shot's prompt inputs + frame_stale + frame_stale_rev,
    taken when the prompt is assembled (B4R3-M1, B4R4-M1). Compared again
    under the Shot lock."""
    from sqlalchemy.orm import object_session
    from sqlalchemy.sql import ClauseElement

    if isinstance(shot.frame_stale_rev, ClauseElement):
        # mark_frame_stale() in this session, not flushed yet: read the value.
        object_session(shot).flush()
    snap = {key: copy.deepcopy(getattr(shot, key, None)) for key in FRAME_PROMPT_FIELDS}
    snap["frame_stale"] = bool(shot.frame_stale)
    snap["frame_stale_rev"] = int(shot.frame_stale_rev or 0)
    return snap


def _set_shot_generating(db: Session, shot, prompt_inputs: dict[str, Any]) -> None:
    """Pending transition of a frame take (enqueue): lock + recompute the shot.

    A fresh generation also resolves the stale-frame warning — but only when
    the shot under the lock still has the inputs the prompt was assembled
    from (`prompt_inputs`, B4R3-M1); a concurrent edit keeps its warning.
    """
    mark_frame_shot_pending(db, shot.id, clear_stale=True, prompt_inputs=prompt_inputs)


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
    if seed is not None and isinstance((params or {}).get("snapshot"), dict):
        # The snapshot records this row's own seed (ADR-0014).
        params = {**params, "snapshot": provider_input.with_seed(params["snapshot"], seed)}
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
    project_id = _project_id_of(db, shot)
    # Budget lock first (lock order Project -> generation -> Shot, ADR-0013
    # B5): the seed pin below is this transaction's first row write.
    budget_guard.lock_project(db, project_id)
    # P1-3 seed resolution: an explicit seed pins shot.seed (fixed from now
    # on); otherwise the shot's pinned seed is reused; otherwise a fresh
    # random seed is drawn for this call (not pinned).
    seed_source = "explicit" if seed is not None else "pinned" if shot.seed is not None else "random"
    if seed is not None:
        shot.seed = seed
    elif shot.seed is not None:
        seed = shot.seed
    else:
        seed = random.randrange(2**31)
    resolved_model = _resolve_model_id(db, shot, model_id)

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

    prompt_inputs = frame_prompt_inputs(shot)  # what the assembly below reads
    assembled = prompt_assembly.assemble_shot_prompt(
        db,
        shot_id,
        overrides=overrides,
        continuity_summary=continuity_summary,
        continuity_image_path=continuity_image,
        continuity_source_id=(continuity_info.get("source_generation_id") or None),
        model_id=resolved_model,
    )
    # ADR-0014: capability pre-check + resolved provider inputs (references
    # with paths/roles, seed, size) BEFORE any budget reservation / job row.
    snapshot = provider_input.image_snapshot(
        db, model_id=resolved_model, task="text_to_image", seed=seed,
        seed_source=seed_source, negative_prompt=assembled["negative_final"],
        references=assembled["references"], style_kit_id=assembled["style_kit_id"],
        style_kit_version=assembled["style_kit_version"],
    )
    per_image = cost_estimator.estimate_images(db, resolved_model, 1)
    total = cost_estimator.estimate_images(db, resolved_model, batch_size)
    budget_guard.check_budget(db, project_id, estimated_new=total)  # 403 BUDGET_EXCEEDED

    params_base: dict[str, Any] = {
        "truncated_sections": assembled["truncated_sections"],
        "continuity": continuity_info,
        "snapshot": snapshot,
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

    _set_shot_generating(db, shot, prompt_inputs)
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
    marked prompt variant and the SAME seed (one variable at a time, F15).
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
    project_id = _project_id_of(db, shot)
    # Budget lock first (lock order Project -> generation -> Shot, ADR-0013
    # B5): the seed pin below is this transaction's first row write.
    budget_guard.lock_project(db, project_id)
    # Same seed semantics as enqueue_frame (explicit pins, else pinned, else random).
    seed_source = "explicit" if seed is not None else "pinned" if shot.seed is not None else "random"
    base_seed = seed if seed is not None else shot.seed
    if base_seed is None:
        base_seed = random.randrange(2**31)
    if seed is not None:
        shot.seed = seed
    resolved_model = _resolve_model_id(db, shot, model_id)

    prompt_inputs = frame_prompt_inputs(shot)  # what the assembly below reads
    assembled = prompt_assembly.assemble_shot_prompt(
        db,
        shot_id,
        model_id=resolved_model,
    )
    snapshot = provider_input.image_snapshot(  # ADR-0014 pre-check + snapshot
        db, model_id=resolved_model, task="text_to_image", seed=base_seed,
        seed_source=seed_source, negative_prompt=assembled["negative_final"],
        references=assembled["references"], style_kit_id=assembled["style_kit_id"],
        style_kit_version=assembled["style_kit_version"],
    )
    per_image = cost_estimator.estimate_images(db, resolved_model, 1)
    total = cost_estimator.estimate_images(db, resolved_model, count)
    budget_guard.check_budget(db, project_id, estimated_new=total)  # 403 BUDGET_EXCEEDED

    params_base: dict[str, Any] = {
        "truncated_sections": assembled["truncated_sections"],
        "continuity": {"available": False, "used": False},
        "variation_axis": variation_axis,
        "snapshot": snapshot,
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
            # One variable at a time (F15, P1-3): the prompt varies, the
            # seed stays fixed so the variants are comparable.
            child_seed = base_seed
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

    _set_shot_generating(db, shot, prompt_inputs)
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
    prompt_inputs = frame_prompt_inputs(shot)
    src = _source_take(db, shot, generation_id)
    mask_rel = _resolve_rel_path(mask_path)
    resolved_model = _resolve_model_id(db, shot, model_id, task="inpaint")
    seed = provider_input.draw_seed()  # persisted on the row (reproducible)
    snapshot = provider_input.image_snapshot(  # 4xx before budget/rows (ADR-0014)
        db, model_id=resolved_model, task="inpaint", seed=seed, seed_source="random",
        init_image=src.output_path, mask=mask_rel,
    )
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
        seed=seed,
        params={"source_generation_id": str(src.id), "mask_path": mask_rel, "snapshot": snapshot},
    )
    _set_shot_generating(db, shot, prompt_inputs)
    _dispatch(db, gen.id)
    db.flush()
    return {"job_id": gen.id, "estimated_cost_usd": cost, "model_id": resolved_model}


def enqueue_upscale(
    db: Session, shot_id: uuid.UUID, *, generation_id: uuid.UUID, model_id: str | None = None
) -> dict[str, Any]:
    from app.services import cost_estimator

    _check_queue_full(db)
    shot = _get_shot(db, shot_id)
    prompt_inputs = frame_prompt_inputs(shot)
    src = _source_take(db, shot, generation_id)
    resolved_model = _resolve_model_id(db, shot, model_id, task="upscale")
    seed = provider_input.draw_seed()  # persisted on the row (reproducible)
    snapshot = provider_input.image_snapshot(  # 4xx before budget/rows (ADR-0014)
        db, model_id=resolved_model, task="upscale", seed=seed, seed_source="random",
        init_image=src.output_path,
    )
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
        seed=seed,
        params={"source_generation_id": str(src.id), "snapshot": snapshot},
    )
    _set_shot_generating(db, shot, prompt_inputs)
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
    prompt_inputs = frame_prompt_inputs(shot)
    src = _source_take(db, shot, generation_id)
    mask_rel = _resolve_rel_path(mask_path) if mask_path else None
    resolved_model = _resolve_model_id(db, shot, model_id, task="outpaint")
    seed = provider_input.draw_seed()  # persisted on the row (reproducible)
    snapshot = provider_input.image_snapshot(  # 4xx before budget/rows (ADR-0014)
        db, model_id=resolved_model, task="outpaint", seed=seed, seed_source="random",
        init_image=src.output_path, mask=mask_rel,
    )
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
            "snapshot": snapshot,
        },
        seed=seed,
    )
    _set_shot_generating(db, shot, prompt_inputs)
    _dispatch(db, gen.id)
    db.flush()
    return {"job_id": gen.id, "estimated_cost_usd": cost, "model_id": resolved_model}


def _terminal(status: str) -> bool:
    return status in ("succeeded", "done", "failed", "canceled")


# --- dependent-state recompute (shared with tasks_image / reconcile) ----------


# Lock order (ADR-0013 B4a-fix, deadlock-free across worker success/failure,
# cancel, retry and reconcile): child generation row(s) -> batch parent row
# -> Video row -> Shot row. Every child row a transaction will write is
# updated BEFORE it locks the parent, and the shot is locked last. The locks
# are row locks (SELECT ... FOR NO KEY UPDATE on Postgres; SQLite serializes
# writers anyway) taken BEFORE sibling states are read, so concurrent
# terminal commits of two children cannot both miss each other (P07).
# Pending transitions (enqueue / retry / reconcile repair) follow the same
# order and ALWAYS lock + reload the shot (mark_frame_shot_pending,
# videos.mark_video_shot_pending), so a terminal recompute of another job
# cannot settle the shot from a stale view of a just-requeued one (R2-H1).
# Style extraction: ledger generation -> style_extractions -> style kit
# (style_extract module docstring, R2-M1).


def lock_row(db: Session, model: Any, row_id: Any) -> Any:
    """Row-lock and reload one row inside the caller's transaction.

    Postgres: SELECT ... FOR NO KEY UPDATE. SQLite has no row locks and
    ignores FOR UPDATE, so a read-then-write (e.g. request_qc's MAX(seq) + 1)
    could interleave with another connection's (B4R4-M2). There a no-op
    write on the row (`UPDATE t SET pk = pk WHERE pk = :id`) runs first: it
    takes SQLite's database write lock (RESERVED) for the rest of the
    transaction, so a concurrent writer waits (busy timeout) before any of
    its own reads that follow its lock_row.
    """
    db.flush()  # never let populate_existing drop pending changes
    if db.get_bind().dialect.name == "sqlite":
        _sqlite_write_lock(db, model, row_id)
    return db.get(
        model, row_id, with_for_update={"key_share": True}, populate_existing=True
    )


def _sqlite_write_lock(db: Session, model: Any, row_id: Any) -> None:
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import update

    (pk,) = sa_inspect(model).mapper.primary_key
    db.execute(update(pk.table).where(pk == row_id).values({pk.name: pk}))


def aggregate_parent(db: Session, parent_id: Any) -> bool:
    """Aggregate child states into a batch parent (D-25) under its row lock.

    Returns True when this call moved the parent to a terminal state.
    """
    from datetime import datetime, timezone

    from app.models.generation import Generation

    parent = lock_row(db, Generation, parent_id)
    if parent is None:
        return False
    states = [
        status
        for (status,) in db.query(Generation.status).filter(
            Generation.parent_generation_id == parent.id
        )
    ]
    if not states:
        return False
    changed = False
    if all(_terminal(s) for s in states):
        if not _terminal(parent.status):
            changed = True
        parent.status = "succeeded" if all(s == "succeeded" for s in states) else "failed"
        parent.finished_at = parent.finished_at or datetime.now(timezone.utc)
        parent.progress_pct = 100.0
    elif any(s in ("queued", "running") for s in states):
        if parent.status in ("queued", "failed"):
            parent.status = "running"
            parent.finished_at = None
    db.flush()
    return changed


def aggregate_batch_parent(db: Session, gen: Any) -> None:
    """Aggregate a child's batch parent (no-op for non-batch generations)."""
    if gen.parent_generation_id is None:
        return
    aggregate_parent(db, gen.parent_generation_id)


def recompute_frame_shot_status(db: Session, shot_id: uuid.UUID) -> bool:
    """Recompute a shot's frame status from its takes once none are pending.

    An existing approval is preserved: new takes never demote frame_approved.
    A shot whose takes were all canceled (none succeeded, none failed) goes
    back to draft instead of failed. The shot row is locked before the takes
    are counted (see lock order above). Returns True when the status changed.
    """
    from app.models.generation import Generation
    from app.models.production import Shot

    shot = lock_row(db, Shot, shot_id)
    if shot is None:
        return False
    takes = db.query(Generation).filter(
        Generation.shot_id == shot.id,
        Generation.is_batch.is_(False),
    )
    if takes.filter(Generation.status.in_(("queued", "running"))).count():
        return False
    if shot.approved_frame_generation_id:
        next_status = "frame_approved"
    elif takes.filter(Generation.status == "succeeded").count():
        next_status = "frame_done"
    elif takes.filter(Generation.status == "failed").count():
        next_status = "failed"
    else:
        next_status = "draft"
    changed = shot.status != next_status
    if changed:
        check_enum("shot_status", next_status)
        shot.status = next_status
    db.flush()
    return changed


# Shot statuses a pending frame take moves to frame_generating (video_* stay).
FRAME_PENDING_FROM = ("draft", "frame_done", "frame_approved", "failed")


def mark_frame_shot_pending(
    db: Session,
    shot_id: uuid.UUID,
    *,
    clear_stale: bool = False,
    prompt_inputs: dict[str, Any] | None = None,
) -> bool:
    """Pending transition (enqueue / retry / reconcile) of a shot's frame take.

    R2-H1: the shot row is locked and reloaded (lock order: child -> batch
    parent -> Video -> Shot, so callers write their child rows first) and
    the decision is made from the CURRENT takes in this transaction — never
    from a cached shot, and never skipped because the shot already looked
    generating. A terminal aggregation of another take therefore either
    commits first (and this call moves the settled shot back to
    frame_generating) or waits for this commit and sees the pending take.
    Returns True when the status changed.

    `clear_stale` with `prompt_inputs` (enqueue, B4R3-M1): the warning is
    cleared only when the locked shot still has the prompt inputs captured
    at assembly time AND nothing marked it stale since (e.g. a character
    appearance edit) — i.e. frame_stale_rev is unchanged (B4R4-M1, also for
    a shot that was already stale). Otherwise an edit committed between
    assembly and this lock keeps its warning: the stored prompt is the
    pre-edit one. Invalidations bump the revision with an UPDATE of the shot
    row, which conflicts with this lock, so the comparison sees every
    invalidation committed before it.
    """
    from app.models.generation import Generation
    from app.models.production import Shot
    from app.services import job_execution

    shot = lock_row(db, Shot, shot_id)
    if shot is None:
        return False
    if clear_stale and _stale_resolved_by(shot, prompt_inputs):
        shot.frame_stale = False
    changed = False
    if shot.status in FRAME_PENDING_FROM:
        pending = (
            db.query(Generation.id)
            .filter(
                Generation.shot_id == shot.id,
                Generation.is_batch.is_(False),
                Generation.job_type.in_(job_execution.IMAGE_JOB_TYPES),
                Generation.status.in_(("queued", "running")),
            )
            .count()
        )
        if pending:
            check_enum("shot_status", "frame_generating")
            shot.status = "frame_generating"
            changed = True
    db.flush()
    return changed


def _stale_resolved_by(shot, prompt_inputs: dict[str, Any] | None) -> bool:
    """True when a take assembled from `prompt_inputs` resolves the shot's
    stale warning (shot = the locked, reloaded row). None = no snapshot
    (retry / reconcile paths): unchanged behaviour, cleared."""
    if prompt_inputs is None:
        return True
    current = frame_prompt_inputs(shot)
    if any(current[k] != prompt_inputs[k] for k in FRAME_PROMPT_FIELDS):
        return False  # a prompt edit committed after assembly
    if current["frame_stale_rev"] != prompt_inputs["frame_stale_rev"]:
        return False  # invalidated after assembly (also stale True -> True)
    # Marked stale after assembly without a shot-field change or a revision
    # bump (a direct frame_stale write that bypassed Shot.mark_frame_stale).
    return not (current["frame_stale"] and not prompt_inputs["frame_stale"])


# --- retry / cancel (ADR-0013) -------------------------------------------------


def _retry_dependents(db: Session, target: Any) -> None:
    """Put rows that depend on a re-queued job back into their pending state."""
    from app.services import job_execution

    job_type = target.job_type or ""
    if job_type in job_execution.VIDEO_JOB_TYPES:
        from app.services import videos as video_service

        video_service.on_video_job_retried(db, target)
    elif job_type == "style_extract":
        from app.services import style_extract as style_service

        style_service.on_ledger_retried(db, target)
    elif job_type == "lora_train":
        from app.models.lora import LoraModel

        lora_id = (target.params or {}).get("lora_model_id")
        lora = db.get(LoraModel, uuid.UUID(str(lora_id))) if lora_id else None
        if lora is not None and lora.status != "ready":
            lora.status = "training"
    elif job_type in job_execution.IMAGE_JOB_TYPES:
        # Lock order: parent before shot (see aggregate_parent); the shot is
        # locked + recomputed even when it already looks generating (R2-H1).
        aggregate_batch_parent(db, target)
        if target.shot_id is not None and not target.is_batch:
            mark_frame_shot_pending(db, target.shot_id, clear_stale=True)
    db.flush()


def _requeue_failed(db: Session, model: Any, row: Any, extra: dict[str, Any]) -> bool:
    """CAS failed -> queued with every execution field cleared."""
    from sqlalchemy import func, update

    from app.services import job_execution

    values = job_execution.requeue_values()
    values.update(
        provider_job_id=None,
        error_code=None,
        error_message=None,
        # A manual retry starts a fresh automatic-retry budget; the history
        # stays in generation_attempts (or audio params) — D7.
        retry_count=0,
        # The new attempt is reserved in the CURRENT budget period (B5 H1).
        reserved_at=func.now(),
        **extra,
    )
    result = db.execute(
        update(model)
        .where(model.id == row.id, model.status == "failed")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.refresh(row)
    return True


def _retry_audio(db: Session, row: Any) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.models.audio import AudioGeneration
    from app.services import job_execution

    # Budget lock before any write (plan D1); re-read the row under it so a
    # concurrent retry of the same row is seen.
    budget_guard.lock_project(db, row.project_id)
    db.refresh(row)
    if row.status != "failed":
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Only failed jobs can be retried.",
            details={"status": row.status},
        )
    uncertain = row.error_code == ErrorCode.PROVIDER_UNCERTAIN
    # Re-reserve the new attempt: 403 BUDGET_EXCEEDED before any row write.
    # An uncertain attempt is still counted here (failed + uncertain) and
    # keeps that reservation as a budget hold once the row is re-queued (D7).
    budget_guard.check_budget(db, row.project_id, estimated_new=row.estimated_cost_usd or 0)
    params = dict(row.params or {})
    if uncertain:
        # Keep the uncertain attempt on record (audio has no attempts table).
        history = list(params.get("_attempt_history") or [])
        history.append({
            "provider_job_id": row.provider_job_id,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        params["_attempt_history"] = history
    held_period = row.reserved_at  # the uncertain attempt's budget period
    if not _requeue_failed(db, AudioGeneration, row, {"params": params or None}):
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Only failed jobs can be retried.")
    if uncertain:
        budget_guard.add_hold(
            db, row.project_id, source_kind="audio", source_id=row.id,
            amount_usd=row.estimated_cost_usd, period_at=held_period,
        )
    job_execution.schedule_dispatch(db, job_execution.AUDIO_TASK, str(row.id))
    db.flush()
    return {"job_id": row.id, "estimated_cost_usd": row.estimated_cost_usd}


def retry_job(db: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Re-queue a failed job (or a batch parent's failed children).

    Only ``failed`` rows are retried. The task is chosen by job_type
    (job_execution.JOB_TASKS: image -> q.image, video -> q.video,
    tts -> q.audio, lora/style/multishot -> q.p1.*), execution fields are
    cleared and dependent rows (Video, Shot, LoRA, batch parent) return to
    their pending state. An audio job id (dialogue/narration TTS) is
    accepted too. Raises 404 when the job is missing, 400 VALIDATION_ERROR
    when nothing is retryable.
    """
    from app.models.audio import AudioGeneration
    from app.models.generation import Generation
    from app.services import job_execution

    job = db.get(Generation, job_id)
    if job is None:
        audio = db.get(AudioGeneration, job_id)
        if audio is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Job not found.")
        return _retry_audio(db, audio)
    # Budget lock before any write (plan D1; lock order Project -> generation
    # -> batch parent -> Video -> Shot). Targets are re-read under it.
    budget_guard.lock_project(db, job.project_id)
    db.refresh(job)
    targets = (
        db.query(Generation)
        .populate_existing()
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
    routes = [job_execution.dispatch_target(t) for t in targets]  # 400 before any write
    for target in targets:
        if target.job_type == "style_extract":
            from app.services import style_extract as style_service

            style_service.require_job_row(db, target)  # 400 before any write (R2-M3)
    # Every re-queued attempt is reserved again: 403 BUDGET_EXCEEDED before
    # any row write. A PROVIDER_UNCERTAIN attempt is still counted here
    # (failed + uncertain) and keeps that reservation as a budget hold once
    # its row is back in the queue (D7/RH1).
    budget_guard.check_budget(
        db, job.project_id,
        estimated_new=sum((Decimal(str(t.estimated_cost_usd or 0)) for t in targets), Decimal(0)),
    )
    requeued = []
    for target, route in zip(targets, routes):
        # A PROVIDER_UNCERTAIN attempt keeps its generation_attempts row
        # (error_code/provider_job_id); the next claim opens a NEW attempt.
        uncertain = target.error_code == ErrorCode.PROVIDER_UNCERTAIN
        held_period = target.reserved_at  # the uncertain attempt's budget period
        extra = {"progress_pct": 0.0, "started_at": None, "finished_at": None}
        if _requeue_failed(db, Generation, target, extra):
            if uncertain:
                budget_guard.add_hold(
                    db, target.project_id, source_kind="generation", source_id=target.id,
                    amount_usd=target.estimated_cost_usd, period_at=held_period,
                )
            requeued.append((target, route))
    # Dependents only after every child row is written (lock order).
    for target, (task_name, target_id) in requeued:
        _retry_dependents(db, target)
        job_execution.schedule_dispatch(db, task_name, target_id)
    retried = len(requeued)
    if not retried:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Only failed jobs can be retried.")
    if job.is_batch:
        job.status = "queued"
        job.finished_at = None
    db.flush()
    return {"job_id": job.id, "estimated_cost_usd": job.estimated_cost_usd}


def _cancel_values(now: Any) -> dict[str, Any]:
    # exec_token cleared: a running worker's next guarded write hits 0 rows,
    # so it stops, cancels the provider job and never overwrites the cancel.
    return {
        "status": "canceled",
        "error_code": "canceled",
        "error_message": "Canceled by user.",
        "exec_token": None,
        "lease_expires_at": None,
    }


def _cancel_audio_row(db: Session, row: Any, now: Any) -> bool:
    from sqlalchemy import update

    from app.models.audio import AudioGeneration

    result = db.execute(
        update(AudioGeneration)
        .where(AudioGeneration.id == row.id, AudioGeneration.status.in_(("queued", "running")))
        .values(**_cancel_values(now))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.refresh(row)
    return True


def cancel_job(db: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Cancel a queued/running job (and queued/running batch children).

    Cascades: Video -> failed (code 'canceled') and the shot leaves
    video_generating; frame takes recompute the shot; a linked audio row
    (tts) is canceled too. Accepts an audio job id as well.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.models.audio import AudioGeneration
    from app.models.generation import Generation
    from app.services import job_execution

    now = datetime.now(timezone.utc)
    job = db.get(Generation, job_id)
    if job is None:
        audio = db.get(AudioGeneration, job_id)
        if audio is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Job not found.")
        if not _cancel_audio_row(db, audio, now):
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "Only queued or running jobs can be canceled.",
                details={"status": audio.status},
            )
        db.flush()
        get_bridge().publish(CHANNEL, "job.canceled", {"job_id": str(audio.id), "job_type": "tts"})
        return {"job_id": audio.id}

    targets = (
        db.query(Generation)
        .filter(Generation.parent_generation_id == job.id)
        .all()
        if job.is_batch
        else [job]
    )
    canceled = []
    for target in targets:
        if _terminal(target.status):
            continue
        result = db.execute(
            update(Generation)
            .where(Generation.id == target.id, Generation.status.in_(("queued", "running")))
            .values(finished_at=now, **_cancel_values(now))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        db.refresh(target)
        canceled.append(target)
    canceled_any = bool(canceled)
    # Dependents only after every child row is written (lock order:
    # children -> parent -> video -> shot, see aggregate_parent).
    for target in canceled:
        job_execution.on_generation_terminal(db, target, success=False)
        audio_id = (target.params or {}).get("audio_generation_id")
        audio = db.get(AudioGeneration, uuid.UUID(str(audio_id))) if audio_id else None
        if audio is not None:
            _cancel_audio_row(db, audio, now)
    if not canceled_any:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Only queued or running jobs can be canceled.",
            details={"status": job.status},
        )
    if job.is_batch:
        # R2-M4: decide from the children's CURRENT states under the parent
        # lock — a child whose cancel CAS hit 0 rows finished concurrently,
        # so the states cached above are stale. The parent is already locked
        # by aggregate_parent above (canceled children are image takes), so
        # this re-lock keeps the lock order.
        parent = lock_row(db, Generation, job.id)
        states = [
            status
            for (status,) in db.query(Generation.status).filter(
                Generation.parent_generation_id == job.id
            )
        ]
        if parent is not None and states and all(_terminal(s) for s in states):
            parent.status = "canceled"
            parent.finished_at = now
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
