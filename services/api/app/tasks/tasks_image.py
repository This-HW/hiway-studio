"""Image queue worker (q.image, concurrency 4).

Task signature is fixed: run_generation(generation_id: str) — single argument.
The DB row is the source of truth; prompt text and parameters are never passed
as task arguments (ARCHITECTURE §8).

Pipeline: route -> submit -> poll -> fetch_result -> save file ->
update row -> aggregate batch parent -> update shot status -> SSE events.

Retry policy (§12.2): retryable failures BEFORE the provider submit intent
are retried up to defaults.RETRY_MAX_ATTEMPTS with exponential backoff; every
execution is recorded as a GenerationAttempt row (F-07). Execution ownership,
failure persistence and the D7 submit-intent rule live in
services/job_execution.py (ADR-0013): once submit intent is committed the job
is never auto-resubmitted (unknown outcome -> PROVIDER_UNCERTAIN).
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone

from celery import Task

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import session_scope
from app.files import immutability, naming, paths
from app.models.generation import Generation
from app.models_lib import capabilities, contract, media_check, registry, router
from app.core.celery_app import celery_app, register_job_dispatcher
from app.services import job_execution

log = logging.getLogger("ai_studio.tasks.image")

def _now():
    return datetime.now(timezone.utc)


def _write_once(abs_path, data: bytes) -> None:
    immutability.write_new(paths.ensure_parent(abs_path), data)


def _thumb_rel(abs_path) -> str | None:
    """Sibling thumbnail of a just-written image (None when it cannot be made)."""
    from app.media.thumbnails import make_image_thumbnail, thumbnail_path_for

    thumb_abs = thumbnail_path_for(abs_path)
    try:
        make_image_thumbnail(abs_path, thumb_abs)
    except StudioError:
        log.warning("thumbnail skipped for %s", abs_path.name)
        return None
    return paths.assert_relative(paths.to_relative(thumb_abs))


def _save_character_image(gen: Generation, data: bytes, char_params: dict, tag: str) -> dict:
    """Save a character master/sheet image (Phase-1 character pipeline, D-18/D-19).

    Files only: the reference row is created by the token-guarded success
    transaction (`reference_for_job`). Names are per execution attempt
    (`tag` = exec token prefix) so a lost or failed attempt's file is an
    orphan that never blocks a retry (ADR-0013 B4a-fix).
    """
    char_dir = str(char_params["_character_dir"])
    role = str(char_params["_reference_role"])
    root = paths.projects_root()
    if role == "master":
        # ADR-0005: the first master keeps the stable master.png path while
        # it is free. It is taken by exclusive create, so a concurrent master
        # or an orphan from a lost attempt makes this attempt fall back to a
        # per-attempt name instead of failing (ADR-0013 B4a-fix).
        first = f"{char_dir}/master.png"
        if not (root / first).exists():
            try:
                _write_once(root / first, data)
                rel_name = first
            except immutability.ImmutableViolation:
                rel_name = None
        else:
            rel_name = None
        if rel_name is None:
            rel_name = f"{char_dir}/master_{gen.id.hex[:8]}_{tag}.png"
            _write_once(root / rel_name, data)
    else:
        rel_name = f"{char_dir}/references/{role}_{gen.id.hex[:8]}_{tag}.png"
        _write_once(root / rel_name, data)
    rel_path = paths.assert_relative(rel_name)
    _thumb_rel(root / rel_name)  # character thumbnails live next to the image
    return {"output_path": rel_path, "thumbnail_path": None, "character": True}


def _attempt_filename(gen: Generation, tag: str, ext: str) -> str:
    """`{generation_id}_{batch_index}_{attempt tag}.{ext}` (§11.2 + ADR-0013)."""
    return naming.single_filename(f"{gen.id}_{gen.batch_index or 0}_{tag}", ext)


def _save_shot_image(db, gen: Generation, data: bytes, ext: str, tag: str) -> dict:
    """Save a shot frame take (frame batch child, inpaint, upscale, outpaint).

    Location: projects/{project_uuid}/scenes/{scene_uuid}/shots/{shot_uuid}/frames/
    (ARCHITECTURE §11), one file per execution attempt.
    """
    from app.models.production import Scene, Shot

    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found for generation.")
    scene = db.get(Scene, shot.scene_id)
    scene_id = scene.id if scene is not None else shot.scene_id
    rel_dir = f"projects/{gen.project_id}/scenes/{scene_id}/shots/{shot.id}/frames"
    filename = _attempt_filename(gen, tag, ext)
    abs_path = paths.projects_root() / rel_dir / filename
    _write_once(abs_path, data)
    return {
        "output_path": paths.assert_relative(f"{rel_dir}/{filename}"),
        "thumbnail_path": _thumb_rel(abs_path),
    }


def _save_thumbnail_image(gen: Generation, data: bytes, thumb_params: dict, tag: str) -> dict:
    """Save a video thumbnail (P1-9, design §10.5, §11.1).

    Location: projects/{project_uuid}/thumbnails/{generation_uuid}_{batch_index}_{tag}.png.
    The title text is composited as a post-processing overlay here — never by
    the image model.
    """
    from app.media.thumbnail_text import render_thumbnail

    rel_dir = f"projects/{gen.project_id}/thumbnails"
    final = render_thumbnail(data, (thumb_params or {}).get("title_text"))
    filename = _attempt_filename(gen, tag, "png")
    abs_path = paths.projects_root() / rel_dir / filename
    _write_once(abs_path, final)
    return {
        "output_path": paths.assert_relative(f"{rel_dir}/{filename}"),
        "thumbnail_path": _thumb_rel(abs_path),
    }


def _legacy_snapshot(gen: Generation) -> dict:
    """Contract inputs of a row enqueued before ADR-0014 (no snapshot):
    editing source -> init_image (+ mask), t2i input_paths -> references."""
    params = gen.params or {}
    inputs = [p for p in (getattr(gen, "input_paths", None) or []) if p]
    snap: dict = {
        "task": gen.job_type or "text_to_image",
        # Still-image size: no resolution column; params may carry one.
        "width": params.get("width"),
        "height": params.get("height"),
        "references": [],
        "references_applied": False,
        "init_image": None,
        "mask": None,
    }
    if capabilities.canonical_task(snap["task"]) in capabilities.EDITING_TASKS:
        snap["init_image"] = inputs[0] if inputs else None
        snap["mask"] = params.get("mask_path") or None
    elif inputs:
        snap["references"] = [contract.reference_entry(p, "reference") for p in inputs]
        snap["references_applied"] = True
    return snap


def _adapter_payload(db, gen: Generation) -> dict:
    """Provider input contract payload (ADR-0014, models_lib/contract.py).

    Built from the enqueue snapshot (``params["snapshot"]``): reference
    image paths + roles/weights, init image, mask, size. Stored paths are
    relative; they are resolved here and must stay inside the projects root.
    The row's seed is authoritative; a row without one (pre-ADR-0014) gets a
    fresh seed here that the caller persists before submit.
    """
    root = paths.projects_root().resolve()

    def _abs(rel: str) -> str:
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "Provider input path escapes the projects root.",
                details={"path": rel},
            ) from None
        return str(target)

    snapshot = (gen.params or {}).get(contract.SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        snapshot = _legacy_snapshot(gen)
    seed = gen.seed if gen.seed is not None else snapshot.get("seed")
    if seed is None:
        # Provider templates (e.g. ComfyUI KSampler) require a concrete {seed}.
        seed = random.randrange(defaults.IMAGE_SEED_MAX_EXCLUSIVE)
    return contract.image_payload(
        snapshot,
        prompt=gen.prompt_final or "",
        negative_prompt=gen.negative_final,
        seed=seed,
        resolve=_abs,
    )


def _executed_config(gen: Generation, model: dict) -> dict:
    """What actually runs (B7-r2 M4): the model entry the worker resolved
    (DB overlay — the same source as the enqueue pre-check) and passes to
    the adapter, next to the enqueue snapshot's view of it.

    Policy: the current registry config executes (an old workflow is not
    kept around to replay); a drift from the snapshot is recorded, never
    hidden, so the snapshot plus this record explain the result.
    """
    snapshot = (gen.params or {}).get(contract.SNAPSHOT_KEY)
    snap = snapshot if isinstance(snapshot, dict) else {}
    cfg_hash = capabilities.config_hash(model)
    recorded = snap.get("model_config_sha256")
    return {
        "model_id": model.get("model_id"),
        "model_version": model.get("version") or f"cfg-{cfg_hash[:12]}",
        "model_config_sha256": cfg_hash,
        "sampler": capabilities.sampler_settings(model),
        "snapshot_model_config_sha256": recorded,
        "config_matches_snapshot": None if recorded is None else (
            recorded == cfg_hash and snap.get("model_id") == model.get("model_id")
        ),
    }


def _progress(ex, generation_id: str, job_type: str, pct: float, stage: str) -> None:
    """Guarded progress write (also a heartbeat + cancel check), then SSE."""
    job_execution.require(ex, {"progress_pct": pct})
    get_bridge().publish(
        CHANNEL,
        "job.progress",
        {
            "job_id": generation_id,
            "job_type": job_type,
            "status": "running",
            "progress_pct": pct,
            "stage": stage,
        },
    )


def _save_output(db, gen: Generation, data: bytes, tag: str) -> dict:
    """Write this attempt's immutable output file(s); no DB writes.

    Returns {"output_path", "thumbnail_path", ["character"]}. The paths are
    published only by the token-guarded success UPDATE (ADR-0013 B4a-fix):
    a lost or failed attempt leaves an unreferenced orphan, never a file in
    the way of the next attempt.
    """
    ext = defaults.FORMAT_IMAGE_GEN
    char_params = gen.params or {}
    if "_character_id" in char_params:
        return _save_character_image(gen, data, char_params, tag)
    if "_thumbnail" in char_params:
        # P1-9: title text is a post-processing overlay (§10.5).
        return _save_thumbnail_image(gen, data, char_params["_thumbnail"], tag)
    if gen.shot_id is not None:
        return _save_shot_image(db, gen, data, ext, tag)
    # Phase-0 location for character-less, shot-less generations.
    rel_dir = f"projects/{gen.project_id}/frames"
    filename = _attempt_filename(gen, tag, ext)
    _write_once(paths.projects_root() / rel_dir / filename, data)
    return {"output_path": paths.assert_relative(f"{rel_dir}/{filename}"),
            "thumbnail_path": None}


@celery_app.task(name="q.image.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one image generation end to end. Returns a summary dict.

    Execution ownership (ADR-0013): the row is claimed queued -> running in
    its own transaction; every later write is guarded by the execution
    token. A delivery that cannot claim (terminal, canceled, owned by a live
    execution) is a no-op — the provider is never called (P20).
    """
    gid = uuid.UUID(generation_id)
    ex = job_execution.claim(Generation, gid)
    if ex is None:
        status = job_execution.current_status(Generation, gid)
        if status is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Generation not found.", details={"job_id": generation_id})
        log.info("generation not claimable (status=%s), no-op job_id=%s", status, generation_id)
        return {"generation_id": generation_id, "status": status}

    with session_scope() as db:
        job_type = db.get(Generation, gid).job_type or "text_to_image"
    phase = job_execution.PHASE_PRE
    try:
        with job_execution.keepalive(ex):
            try:
                _progress(ex, generation_id, job_type, 5.0, "routing")
                with session_scope() as db:
                    gen = db.get(Generation, gid)
                    routed = router.route(
                        task=job_type,
                        modality="image",
                        explicit_model_id=gen.model_id,
                        db=db,
                    )
                    payload = _adapter_payload(db, gen)
                    drawn_seed = payload["seed"] if gen.seed is None else None
                    # One config source for pre-check, record and adapter.
                    model = registry.get_model(routed["model_id"], db)
                    executed = _executed_config(gen, model)
                if executed["config_matches_snapshot"] is False:
                    log.warning(
                        "generation %s: model config changed since enqueue "
                        "(snapshot %s, executing %s)", generation_id,
                        str(executed["snapshot_model_config_sha256"])[:12],
                        executed["model_config_sha256"][:12],
                    )

                def _record_executed(db, row: Generation) -> None:
                    row.params = {**(row.params or {}), "executed": executed}

                # Persist a worker-drawn seed and the executed config before
                # submit (reproducible / auditable, ADR-0014 B7-r2).
                job_execution.require(
                    ex, {"seed": drawn_seed} if drawn_seed is not None else {},
                    after=_record_executed,
                )
                adapter = registry.get_adapter(routed["adapter"])
                ex.model_id = routed["model_id"]

                phase = job_execution.PHASE_SUBMITTING
                with registry.pinned_model(model):
                    data = job_execution.submit_poll_fetch(
                        ex, adapter, routed["model_id"], payload,
                        progress=lambda pct, stage: _progress(ex, generation_id, job_type, pct, stage),
                    )
                phase = job_execution.PHASE_LOCAL
                # Only a decodable image is ever published (B7-r2 M3).
                # (No provider id in the message: provider metadata is quoted
                # only through the adapter's redaction gate, B7-r3 M1.)
                media_check.check_image_decodes(data, what="Provider result")
                # Per-attempt immutable file(s) first, outside any write
                # transaction (D8 pattern): the token-guarded UPDATE below is
                # the only publish. A lost/failed attempt leaves an orphan.
                job_execution.require(ex, {})
                with session_scope() as db:
                    saved = _save_output(db, db.get(Generation, gid), data, ex.token.hex[:8])
                result: dict = {}

                def _publish_success(db, gen: Generation) -> None:
                    # Same transaction as the guarded success UPDATE: paths,
                    # reference rows and dependents land only for the owner.
                    gen.output_path = saved["output_path"]
                    if saved.get("thumbnail_path"):
                        gen.output_thumbnail_path = saved["thumbnail_path"]
                    if saved.get("character"):
                        from app.services.character_pipeline import reference_for_job

                        reference_for_job(db, gen, saved["output_path"])
                    job_execution.finish_attempt(db, ex)
                    db.flush()
                    job_execution.on_generation_terminal(db, gen, success=True)
                    result["output_path"] = saved["output_path"]

                done = job_execution.guarded_update(
                    ex,
                    {
                        "status": "succeeded",
                        "progress_pct": 100.0,
                        "model_id": routed["model_id"],
                        "actual_cost_usd": Generation.estimated_cost_usd,
                        "finished_at": _now(),
                        "error_code": None,
                        "error_message": None,
                    },
                    after=_publish_success,
                )
                if not done:
                    raise job_execution.OwnershipLost(generation_id)
            except (job_execution.OwnershipLost, job_execution.JobFailure):
                raise
            except Exception as exc:  # noqa: BLE001 - persisted as a failure below
                raise job_execution.JobFailure(phase, job_execution.as_studio_error(exc)) from exc
    except job_execution.OwnershipLost:
        log.info("generation lost ownership (canceled/superseded) job_id=%s", generation_id)
        return {"generation_id": generation_id, "status": "lost"}
    except job_execution.JobFailure as failure:
        return _handle_failure(self, ex, failure, generation_id, job_type)

    get_bridge().publish(
        CHANNEL,
        "job.completed",
        {
            "job_id": generation_id,
            "job_type": job_type,
            "status": "succeeded",
            "output_path": result["output_path"],
            "output_count": 1,
        },
    )
    log.info("generation succeeded job_id=%s model=%s", generation_id, routed["model_id"])
    return {"generation_id": generation_id, "status": "succeeded", "output_path": result["output_path"]}


def _handle_failure(self: Task, ex, failure, generation_id: str, job_type: str) -> dict:
    """Commit the failure first, then SSE / retry / raise (F02/F03)."""

    def _after_fail(db, gen: Generation, code: str, message: str) -> None:
        job_execution.on_generation_terminal(db, gen, success=False)

    outcome, code = job_execution.record_failure(ex, failure, after_fail=_after_fail)
    exc = failure.error
    if outcome == "lost":
        log.info("generation failure not recorded (ownership lost) job_id=%s", generation_id)
        return {"generation_id": generation_id, "status": "lost"}
    if outcome == "retry":
        log.warning(
            "generation failed, retrying job_id=%s attempt=%s code=%s",
            generation_id, ex.retry_count + 1, code,
        )
        raise self.retry(exc=exc, countdown=job_execution.retry_countdown(ex.retry_count + 1))
    get_bridge().publish(
        CHANNEL,
        "job.failed",
        {
            "job_id": generation_id,
            "job_type": job_type,
            "error_code": code,
            "error_message": exc.message,
            "retryable": False,
            "retry_count": ex.retry_count,
            "max_retries": defaults.RETRY_MAX_ATTEMPTS,
        },
    )
    log.warning("generation failed job_id=%s code=%s", generation_id, code)
    raise exc


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.image.run_generation", run_generation.delay)
