"""Image queue worker (q.image, concurrency 4).

Task signature is fixed: run_generation(generation_id: str) — single argument.
The DB row is the source of truth; prompt text and parameters are never passed
as task arguments (ARCHITECTURE §8).

Pipeline: route -> submit -> poll -> fetch_result -> save file ->
update row -> aggregate batch parent -> update shot status -> SSE events.

Retry policy (§12.2): retryable failures are retried up to
defaults.RETRY_MAX_ATTEMPTS with exponential backoff; every execution is
recorded as a GenerationAttempt row (F-07).
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
from app.models.generation import Generation, GenerationAttempt
from app.models_lib import registry, router
from app.models_lib.adapters.http_polling import poll_until_done
from app.models_lib.base import JobStatus
from app.core.celery_app import celery_app, register_job_dispatcher

log = logging.getLogger("ai_studio.tasks.image")

TERMINAL = ("succeeded", "failed", "canceled")


def _now():
    return datetime.now(timezone.utc)


def _save_character_image(
    db, gen: Generation, data: bytes, char_params: dict, ext: str
) -> str:
    """Save a character master/sheet image and register its reference row.

    Phase-1 character pipeline completion (D-18/D-19). The generation params
    carry _character_id/_reference_role/_source/_character_dir; the reference
    row is created through services.character_pipeline (tasks -> services OK).
    """
    from app.media.thumbnails import make_image_thumbnail, thumbnail_path_for
    from app.services.character_pipeline import reference_for_job

    char_dir = str(char_params["_character_dir"])
    role = str(char_params["_reference_role"])
    if role == "master":
        # ADR-0005: first master keeps the stable master.png path; regenerations
        # get a new file (outputs are immutable, never overwritten).
        first = paths.projects_root() / f"{char_dir}/master.png"
        if first.exists():
            rel_name = f"{char_dir}/master_{gen.id.hex[:8]}.png"
        else:
            rel_name = f"{char_dir}/master.png"
    else:
        rel_name = f"{char_dir}/references/{role}_{gen.id.hex[:8]}.png"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_name)
    immutability.write_new(abs_path, data)
    rel_path = paths.assert_relative(rel_name)
    reference_for_job(db, gen, rel_path)
    try:
        make_image_thumbnail(abs_path, thumbnail_path_for(abs_path))
    except StudioError:
        log.warning("thumbnail skipped for %s", rel_path)
    return rel_path


def _save_shot_image(db, gen: Generation, data: bytes, ext: str) -> str:
    """Save a shot frame take (frame batch child, inpaint, upscale, outpaint).

    Location: projects/{project_uuid}/scenes/{scene_uuid}/shots/{shot_uuid}/frames/
    (ARCHITECTURE §11). Returns the relative path.
    """
    from app.media.thumbnails import make_image_thumbnail, thumbnail_path_for
    from app.models.production import Scene, Shot

    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found for generation.")
    scene = db.get(Scene, shot.scene_id)
    scene_id = scene.id if scene is not None else shot.scene_id
    rel_dir = f"projects/{gen.project_id}/scenes/{scene_id}/shots/{shot.id}/frames"
    filename = naming.frame_filename(str(gen.id), gen.batch_index or 0, ext)
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, data)
    rel_path = paths.assert_relative(f"{rel_dir}/{filename}")
    try:
        thumb_abs = thumbnail_path_for(abs_path)
        make_image_thumbnail(abs_path, thumb_abs)
        gen.output_thumbnail_path = paths.assert_relative(paths.to_relative(thumb_abs))
    except StudioError:
        log.warning("thumbnail skipped for %s", rel_path)
    return rel_path


def _save_thumbnail_image(db, gen: Generation, data: bytes, thumb_params: dict, ext: str) -> str:
    """Save a video thumbnail (P1-9, design §10.5, §11.1).

    Location: projects/{project_uuid}/thumbnails/{generation_uuid}_{batch_index}.png.
    The title text is composited as a post-processing overlay here — never by
    the image model.
    """
    from app.media.thumbnail_text import render_thumbnail
    from app.media.thumbnails import make_image_thumbnail, thumbnail_path_for

    rel_dir = f"projects/{gen.project_id}/thumbnails"
    final = render_thumbnail(data, (thumb_params or {}).get("title_text"))
    filename = naming.frame_filename(str(gen.id), gen.batch_index or 0, "png")
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, final)
    rel_path = paths.assert_relative(f"{rel_dir}/{filename}")
    try:
        thumb_abs = thumbnail_path_for(abs_path)
        make_image_thumbnail(abs_path, thumb_abs)
        gen.output_thumbnail_path = paths.assert_relative(paths.to_relative(thumb_abs))
    except StudioError:
        log.warning("thumbnail skipped for %s", rel_path)
    return rel_path


def _adapter_payload(db, gen: Generation) -> dict:
    """Build the adapter payload for the job type (stub ignores extras)."""
    root = paths.projects_root()

    def _abs(rel: str | None) -> str | None:
        if not rel:
            return None
        return str((root / rel).resolve())

    payload: dict = {
        "prompt": gen.prompt_final or "",
        "negative_prompt": gen.negative_final or "",
        # Still-image size: the Generation model has no resolution field yet
        # (unlike video clips), but provider templates (e.g. ComfyUI
        # EmptyLatentImage) need concrete {width}/{height}. A future
        # per-request resolution parameter can override via gen.params.
        "width": (gen.params or {}).get("width") or defaults.IMAGE_DEFAULT_WIDTH,
        "height": (gen.params or {}).get("height") or defaults.IMAGE_DEFAULT_HEIGHT,
    }
    if gen.seed is not None:
        payload["seed"] = gen.seed
    else:
        # Provider templates (e.g. ComfyUI KSampler) require a concrete {seed};
        # character master/sheet generations don't pin one, so draw a fresh
        # random seed like enqueue_frame does for unpinned shots.
        payload["seed"] = random.randrange(2**31)
    if gen.job_type in ("inpaint", "upscale", "outpaint"):
        inputs = [p for p in (gen.input_paths or [])]
        payload["images"] = [_abs(p) for p in inputs if p]
        mask = (gen.params or {}).get("mask_path")
        if mask:
            payload["mask"] = _abs(mask)
    return payload


def _progress(db, gen: Generation, generation_id: str, job_type: str, pct: float, stage: str) -> None:
    gen.progress_pct = pct
    db.flush()
    get_bridge().publish(
        CHANNEL,
        "job.progress",
        {
            "job_id": generation_id,
            "job_type": job_type,
            "status": gen.status,
            "progress_pct": pct,
            "stage": stage,
        },
    )


def _notify_multishot_frame(db, gen: Generation, success: bool) -> None:
    """Aggregate a finished coverage frame into its multishot set (P1-1).

    Generations created by q.p1.run_multishot carry
    params["_multishot_frame_id"]; every other generation skips this.
    """
    frame_id = (gen.params or {}).get("_multishot_frame_id")
    if not frame_id:
        return
    try:
        from app.services import multishot as multishot_service

        multishot_service.on_frame_finished(db, str(frame_id), success)
    except StudioError:
        log.warning("multishot aggregation skipped for frame %s", frame_id)


def _update_batch_parent(db, gen: Generation) -> None:
    """Aggregate child states into the batch parent (D-25)."""
    if gen.parent_generation_id is None:
        return
    parent = db.get(Generation, gen.parent_generation_id)
    if parent is None:
        return
    children = (
        db.query(Generation)
        .filter(Generation.parent_generation_id == parent.id)
        .all()
    )
    states = [c.status for c in children]
    if all(s in TERMINAL for s in states):
        parent.status = "succeeded" if all(s == "succeeded" for s in states) else "failed"
        parent.finished_at = _now()
        parent.progress_pct = 100.0
    elif any(s in ("queued", "running") for s in states):
        if parent.status == "queued":
            parent.status = "running"
    db.flush()


def _update_shot_status(db, gen: Generation) -> None:
    """Recompute the shot status from its takes once none are pending.

    An existing approval is preserved: new takes never demote frame_approved.
    """
    if gen.shot_id is None:
        return
    from app.core.contracts import check_enum
    from app.models.production import Shot

    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        return
    pending = (
        db.query(Generation)
        .filter(
            Generation.shot_id == shot.id,
            Generation.is_batch.is_(False),
            Generation.status.in_(("queued", "running")),
        )
        .count()
    )
    if pending:
        return
    if shot.approved_frame_generation_id:
        next_status = "frame_approved"
    else:
        succeeded = (
            db.query(Generation)
            .filter(
                Generation.shot_id == shot.id,
                Generation.is_batch.is_(False),
                Generation.status == "succeeded",
            )
            .count()
        )
        next_status = "frame_done" if succeeded else "failed"
    if shot.status != next_status:
        check_enum("shot_status", next_status)
        shot.status = next_status
    db.flush()


@celery_app.task(name="q.image.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one image generation end to end. Returns a summary dict."""
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(generation_id))
        if gen is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Generation not found.", details={"job_id": generation_id})

        # Canceled while queued: never run the provider call.
        if gen.status == "canceled":
            log.info("generation canceled before run job_id=%s", generation_id)
            return {"generation_id": generation_id, "status": "canceled"}

        job_type = gen.job_type or "text_to_image"
        attempt = GenerationAttempt(
            generation_id=gen.id,
            attempt_no=gen.retry_count + 1,
            model_id=gen.model_id or "",
            started_at=_now(),
        )
        db.add(attempt)
        gen.status = "running"
        gen.started_at = gen.started_at or _now()
        db.flush()

        try:
            _progress(db, gen, generation_id, job_type, 5.0, "routing")
            routed = router.route(
                task=job_type,
                modality="image",
                explicit_model_id=gen.model_id,
                db=db,
            )
            adapter = registry.get_adapter(routed["adapter"])
            attempt.model_id = routed["model_id"]

            _progress(db, gen, generation_id, job_type, 20.0, "submitted")
            provider_job_id = adapter.submit(routed["model_id"], _adapter_payload(db, gen))
            attempt.provider_job_id = provider_job_id

            def _on_poll(status: JobStatus) -> None:
                _progress(
                    db, gen, generation_id, job_type,
                    20.0 + status.progress_pct * 0.5, status.stage or "polling",
                )

            final = poll_until_done(adapter, provider_job_id, on_progress=_on_poll)
            if final.state != "succeeded":
                raise StudioError(
                    final.error_code or ErrorCode.PROVIDER_ERROR,
                    final.error_message or "Provider reported failure.",
                )

            _progress(db, gen, generation_id, job_type, 90.0, "saving")
            data = adapter.fetch_result(provider_job_id)
            ext = defaults.FORMAT_IMAGE_GEN
            char_params = gen.params or {}
            if "_character_id" in char_params:
                rel_path = _save_character_image(db, gen, data, char_params, ext)
            elif "_thumbnail" in char_params:
                # P1-9: title text is a post-processing overlay (§10.5).
                rel_path = _save_thumbnail_image(db, gen, data, char_params["_thumbnail"], ext)
            elif gen.shot_id is not None:
                rel_path = _save_shot_image(db, gen, data, ext)
            else:
                # Phase-0 location for character-less, shot-less generations.
                rel_dir = f"projects/{gen.project_id}/frames"
                filename = naming.frame_filename(str(gen.id), 0, ext)
                abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
                immutability.write_new(abs_path, data)
                rel_path = paths.assert_relative(f"{rel_dir}/{filename}")

            gen.status = "succeeded"
            gen.progress_pct = 100.0
            gen.output_path = rel_path
            gen.model_id = routed["model_id"]
            gen.actual_cost_usd = gen.estimated_cost_usd
            gen.finished_at = _now()
            gen.error_code = None
            gen.error_message = None
            attempt.finished_at = _now()
            db.flush()

            _update_batch_parent(db, gen)
            _update_shot_status(db, gen)
            _notify_multishot_frame(db, gen, success=True)

            get_bridge().publish(
                CHANNEL,
                "job.completed",
                {
                    "job_id": generation_id,
                    "job_type": job_type,
                    "status": "succeeded",
                    "output_path": rel_path,
                    "output_count": 1,
                },
            )
            log.info("generation succeeded job_id=%s model=%s", generation_id, routed["model_id"])
            return {"generation_id": generation_id, "status": "succeeded", "output_path": rel_path}

        except StudioError as exc:
            attempt.finished_at = _now()
            attempt.error_code = exc.code
            attempt.error_message = exc.message
            if exc.retryable and gen.retry_count < defaults.RETRY_MAX_ATTEMPTS:
                gen.retry_count += 1
                gen.status = "queued"
                gen.error_code = exc.code
                gen.error_message = exc.message
                db.flush()
                countdown = (
                    defaults.RETRY_BACKOFF_BASE_SEC * (2 ** (gen.retry_count - 1))
                    + random.uniform(0, defaults.RETRY_JITTER_SEC)
                )
                log.warning(
                    "generation failed, retrying job_id=%s attempt=%s code=%s",
                    generation_id, gen.retry_count, exc.code,
                )
                raise self.retry(exc=exc, countdown=countdown)
            gen.status = "failed"
            gen.finished_at = _now()
            gen.error_code = exc.code
            gen.error_message = exc.message
            db.flush()

            _update_batch_parent(db, gen)
            _update_shot_status(db, gen)
            _notify_multishot_frame(db, gen, success=False)

            get_bridge().publish(
                CHANNEL,
                "job.failed",
                {
                    "job_id": generation_id,
                    "job_type": job_type,
                    "error_code": exc.code,
                    "error_message": exc.message,
            "retryable": exc.retryable,
                    "retry_count": gen.retry_count,
                    "max_retries": defaults.RETRY_MAX_ATTEMPTS,
                },
            )
            log.warning("generation failed job_id=%s code=%s", generation_id, exc.code)
            raise


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.image.run_generation", run_generation.delay)
