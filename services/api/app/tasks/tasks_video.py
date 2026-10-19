"""Video queue worker (q.video, concurrency 2).

Task signature is fixed: run_generation(generation_id: str) — single argument.
The DB row is the source of truth; prompt text and parameters are never passed
as task arguments (ARCHITECTURE §8).

Pipeline: route -> submit -> poll -> fetch_result -> save mp4 -> probe ->
update video row -> update shot status -> SSE events.

Retry policy (§12.2): same as the image worker — retryable failures are
retried up to defaults.RETRY_MAX_ATTEMPTS with exponential backoff; every
execution is recorded as a GenerationAttempt row.
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone

from celery import Task

from app.core import defaults
from app.core.celery_app import celery_app, register_job_dispatcher
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import session_scope
from app.files import immutability, paths
from app.media import ffmpeg_ops
from app.models.generation import Generation, GenerationAttempt
from app.models_lib import registry, router
from app.models_lib.adapters.http_polling import poll_until_done
from app.models_lib.base import JobStatus

log = logging.getLogger("ai_studio.tasks.video")

TERMINAL = ("succeeded", "failed", "canceled")


def _now():
    return datetime.now(timezone.utc)


def _adapter_payload(db, gen: Generation) -> dict:
    """Build the adapter payload (stub ignores extras)."""
    root = paths.projects_root()

    def _abs(rel: str | None) -> str | None:
        if not rel:
            return None
        return str((root / rel).resolve())

    params = gen.params or {}
    payload: dict = {
        "mode": params.get("mode", "i2v"),
        "motion_prompt": params.get("motion_prompt", ""),
        "prompt": gen.prompt_final or "",
        "duration_sec": params.get("duration_sec", 5.0),
    }
    if gen.job_type == "image_to_video":
        inputs = [p for p in (gen.input_paths or [])]
        payload["images"] = [_abs(p) for p in inputs if p]
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


def _save_clip(db, gen: Generation, video, data: bytes) -> str:
    """Save the clip mp4 + a thumbnail still. Returns the relative path."""
    from app.models.production import Scene, Shot

    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found for generation.")
    scene = db.get(Scene, shot.scene_id)
    scene_id = scene.id if scene is not None else shot.scene_id
    rel_dir = f"projects/{gen.project_id}/scenes/{scene_id}/shots/{shot.id}/videos"
    filename = f"{video.id}.mp4"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, data)
    rel_path = paths.assert_relative(f"{rel_dir}/{filename}")
    try:
        thumb_abs = abs_path.with_name(f"{video.id}.thumb.jpg")
        ffmpeg_ops.extract_frame(abs_path, thumb_abs, at_sec=0.5)
        gen.output_thumbnail_path = paths.assert_relative(paths.to_relative(thumb_abs))
    except StudioError:
        log.warning("thumbnail skipped for %s", rel_path)
    return rel_path


def _update_shot_status(db, gen: Generation) -> None:
    """Recompute the shot status once no video job is pending.

    A finished clip never demotes the shot below what the approved frame
    already earned: failure without any done clip falls back to
    frame_approved (the frame is still usable for a retry).
    """
    from app.core.contracts import check_enum
    from app.models.production import Shot
    from app.models.generation import Generation, Video

    if gen.shot_id is None:
        return
    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        return
    pending = (
        db.query(Generation)
        .filter(
            Generation.shot_id == shot.id,
            Generation.job_type.in_(("image_to_video", "text_to_video")),
            Generation.status.in_(("queued", "running")),
        )
        .count()
    )
    if pending:
        return
    done_clips = (
        db.query(Video)
        .filter(Video.shot_id == shot.id, Video.status == "done")
        .count()
    )
    if done_clips:
        next_status = "video_done"
    elif shot.approved_frame_generation_id:
        next_status = "frame_approved"
    else:
        next_status = "failed"
    if shot.status != next_status:
        check_enum("shot_status", next_status)
        shot.status = next_status
    db.flush()


@celery_app.task(name="q.video.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one video generation end to end. Returns a summary dict."""
    with session_scope() as db:
        from app.models.generation import Generation, Video

        gen = db.get(Generation, uuid.UUID(generation_id))
        if gen is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Generation not found.", details={"job_id": generation_id})

        if gen.status == "canceled":
            log.info("generation canceled before run job_id=%s", generation_id)
            return {"generation_id": generation_id, "status": "canceled"}

        job_type = gen.job_type or "image_to_video"
        video = (
            db.query(Video).filter(Video.generation_id == gen.id).order_by(Video.created_at.desc()).first()
        )
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
                modality="video",
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
            if video is None:
                raise StudioError(ErrorCode.NOT_FOUND, "Video row not found for generation.")
            rel_path = _save_clip(db, gen, video, data)

            # Probe the actual media facts; fall back to requested values.
            try:
                facts = ffmpeg_ops.probe(paths.projects_root() / rel_path)
            except StudioError:
                facts = {}
                log.warning("probe failed for %s; using requested values", rel_path)
            video.output_path = rel_path
            video.duration_sec = facts.get("duration_sec") or video.duration_sec
            video.fps = int(facts.get("fps") or video.fps)
            if facts.get("width"):
                video.resolution = f"{facts['width']}x{facts['height']}"
            video.model_id = routed["model_id"]
            video.status = "done"
            video.error_code = None
            video.error_message = None

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

            _update_shot_status(db, gen)

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
            log.info("video generation succeeded job_id=%s model=%s", generation_id, routed["model_id"])
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
                    "video generation failed, retrying job_id=%s attempt=%s code=%s",
                    generation_id, gen.retry_count, exc.code,
                )
                raise self.retry(exc=exc, countdown=countdown)
            gen.status = "failed"
            gen.finished_at = _now()
            gen.error_code = exc.code
            gen.error_message = exc.message
            if video is not None:
                video.status = "failed"
                video.error_code = exc.code
                video.error_message = exc.message
            db.flush()

            _update_shot_status(db, gen)

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
            log.warning("video generation failed job_id=%s code=%s", generation_id, exc.code)
            raise


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.video.run_generation", run_generation.delay)
