"""Video queue worker (q.video, concurrency 2).

Task signature is fixed: run_generation(generation_id: str) — single argument.
The DB row is the source of truth; prompt text and parameters are never passed
as task arguments (ARCHITECTURE §8).

Pipeline: route -> submit -> poll -> fetch_result -> save mp4 -> probe ->
update video row -> update shot status -> SSE events.

Retry policy (§12.2): same as the image worker — retryable failures before
the provider submit intent are retried up to defaults.RETRY_MAX_ATTEMPTS with
exponential backoff; every execution is recorded as a GenerationAttempt row.
Ownership / D7 rules: services/job_execution.py (ADR-0013).
"""
from __future__ import annotations

import logging
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
from app.models.generation import Generation
from app.models_lib import media_check, registry, router
from app.services import job_execution

log = logging.getLogger("ai_studio.tasks.video")

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
        if payload["images"]:
            # Provider input contract key (ADR-0014): the start frame.
            payload["init_image"] = payload["images"][0]
    return payload


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


def _save_clip(db, gen: Generation, video, data: bytes, tag: str) -> dict:
    """Write this attempt's clip mp4 + thumbnail still; no DB writes.

    `{video_id}_{tag}.mp4` (tag = exec token prefix): the same Video row is
    reused by a manual retry, so a lost/failed attempt's file must never sit
    on the next attempt's path (ADR-0013 B4a-fix). Returns the relative
    paths and probed media facts; the token-guarded UPDATE publishes them.
    """
    from app.models.production import Scene, Shot

    shot = db.get(Shot, gen.shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found for generation.")
    scene = db.get(Scene, shot.scene_id)
    scene_id = scene.id if scene is not None else shot.scene_id
    rel_dir = f"projects/{gen.project_id}/scenes/{scene_id}/shots/{shot.id}/videos"
    filename = f"{video.id}_{tag}.mp4"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, data)
    rel_path = paths.assert_relative(f"{rel_dir}/{filename}")
    thumb_rel = None
    try:
        thumb_abs = abs_path.with_name(f"{video.id}_{tag}.thumb.jpg")
        ffmpeg_ops.extract_frame(abs_path, thumb_abs, at_sec=0.5)
        thumb_rel = paths.assert_relative(paths.to_relative(thumb_abs))
    except StudioError:
        log.warning("thumbnail skipped for %s", rel_path)
    facts = _validated_facts(abs_path, data, rel_path)
    return {"output_path": rel_path, "thumbnail_path": thumb_rel, "facts": facts}


def _validated_facts(abs_path, data: bytes, rel_path: str) -> dict:
    """Probe the downloaded clip; a clip that cannot be probed FAILS the job
    (audit F16: never substitute the requested values for a broken file).

    Only a missing ffprobe binary (an environment gap, not a property of the
    file) degrades to the magic-byte check with the requested values.
    """
    try:
        facts = ffmpeg_ops.probe(abs_path)
    except StudioError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            media_check.check_media(data, "video", what=f"Downloaded clip {rel_path}")
            log.warning("ffprobe unavailable; %s checked by magic bytes only", rel_path)
            return {}
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"Downloaded clip failed validation (ffprobe): {exc.message}",
            details={"reason": "result_probe_failed", "path": rel_path},
        ) from exc
    if not facts.get("duration_sec"):
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            "Downloaded clip has no duration (not a playable video).",
            details={"reason": "result_probe_failed", "path": rel_path},
        )
    return facts


@celery_app.task(name="q.video.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one video generation end to end. Returns a summary dict.

    Same execution-ownership contract as the image worker (ADR-0013): claim
    or no-op, token-guarded writes, D7 submit intent, failure committed
    before SSE/raise.
    """
    from app.models.generation import Video

    gid = uuid.UUID(generation_id)
    ex = job_execution.claim(Generation, gid)
    if ex is None:
        status = job_execution.current_status(Generation, gid)
        if status is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Generation not found.", details={"job_id": generation_id})
        log.info("video generation not claimable (status=%s), no-op job_id=%s", status, generation_id)
        return {"generation_id": generation_id, "status": status}

    with session_scope() as db:
        job_type = db.get(Generation, gid).job_type or "image_to_video"
    phase = job_execution.PHASE_PRE
    try:
        with job_execution.keepalive(ex):
            try:
                _progress(ex, generation_id, job_type, 5.0, "routing")
                with session_scope() as db:
                    gen = db.get(Generation, gid)
                    routed = router.route(
                        task=job_type,
                        modality="video",
                        explicit_model_id=gen.model_id,
                        db=db,
                    )
                    payload = _adapter_payload(db, gen)
                    # The adapter runs the config the pre-check saw (DB
                    # overlay), not the seed cache (B7-r2 M4).
                    model = registry.get_model(routed["model_id"], db)
                    has_video = (
                        db.query(Video).filter(Video.generation_id == gid).first() is not None
                    )
                if not has_video:
                    raise StudioError(ErrorCode.NOT_FOUND, "Video row not found for generation.")
                adapter = registry.get_adapter(routed["adapter"])
                ex.model_id = routed["model_id"]

                phase = job_execution.PHASE_SUBMITTING
                with registry.pinned_model(model):
                    data = job_execution.submit_poll_fetch(
                        ex, adapter, routed["model_id"], payload,
                        progress=lambda pct, stage: _progress(ex, generation_id, job_type, pct, stage),
                    )
                phase = job_execution.PHASE_LOCAL
                # Per-attempt immutable clip first, outside any write
                # transaction (D8 pattern); the guarded UPDATE publishes it.
                job_execution.require(ex, {})
                with session_scope() as db:
                    gen = db.get(Generation, gid)
                    video = (
                        db.query(Video).filter(Video.generation_id == gid)
                        .order_by(Video.created_at.desc()).first()
                    )
                    if video is None:
                        raise StudioError(ErrorCode.NOT_FOUND, "Video row not found for generation.")
                    video_id = video.id
                    saved = _save_clip(db, gen, video, data, ex.token.hex[:8])
                result: dict = {}

                def _publish_success(db, gen: Generation) -> None:
                    # Same transaction as the guarded success UPDATE (owner
                    # only): the Video row, shot status and paths.
                    video = db.get(Video, video_id)
                    if video is None:
                        raise StudioError(ErrorCode.NOT_FOUND, "Video row not found for generation.")
                    facts = saved["facts"]
                    rel_path = saved["output_path"]
                    video.output_path = rel_path
                    video.duration_sec = facts.get("duration_sec") or video.duration_sec
                    video.fps = int(facts.get("fps") or video.fps)
                    if facts.get("width"):
                        video.resolution = f"{facts['width']}x{facts['height']}"
                    video.model_id = routed["model_id"]
                    video.status = "done"
                    video.error_code = None
                    video.error_message = None
                    gen.output_path = rel_path
                    if saved["thumbnail_path"]:
                        gen.output_thumbnail_path = saved["thumbnail_path"]
                    job_execution.finish_attempt(db, ex)
                    db.flush()
                    job_execution.on_generation_terminal(db, gen, success=True)
                    result["output_path"] = rel_path

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
        log.info("video generation lost ownership (canceled/superseded) job_id=%s", generation_id)
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
    log.info("video generation succeeded job_id=%s model=%s", generation_id, routed["model_id"])
    return {"generation_id": generation_id, "status": "succeeded", "output_path": result["output_path"]}


def _handle_failure(self: Task, ex, failure, generation_id: str, job_type: str) -> dict:
    """Commit the failure (Video failed, shot recomputed) first, then SSE / retry / raise."""

    def _after_fail(db, gen: Generation, code: str, message: str) -> None:
        job_execution.on_generation_terminal(db, gen, success=False)

    outcome, code = job_execution.record_failure(ex, failure, after_fail=_after_fail)
    exc = failure.error
    if outcome == "lost":
        log.info("video failure not recorded (ownership lost) job_id=%s", generation_id)
        return {"generation_id": generation_id, "status": "lost"}
    if outcome == "retry":
        log.warning(
            "video generation failed, retrying job_id=%s attempt=%s code=%s",
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
    log.warning("video generation failed job_id=%s code=%s", generation_id, code)
    raise exc


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.video.run_generation", run_generation.delay)
