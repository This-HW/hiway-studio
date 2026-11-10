"""Audio queue worker (q.audio, concurrency 2).

Task signature is fixed: run_generation(generation_id: str) — single argument,
the *audio* generation id (ADR-0008: one target row id + DB as source of
truth). Prompt text, voice id and emotion weights are never passed as task
arguments.

Pipeline: route -> submit -> poll -> fetch_result -> save wav ->
(optional lipsync fit) -> update row -> SSE events.

Retry policy (§12.2): retryable failures before the provider submit intent
are retried up to defaults.RETRY_MAX_ATTEMPTS with exponential backoff; the
counters live in audio_generations.retry_count / attempt_count (no attempts
table). Ownership / D7 rules: services/job_execution.py (ADR-0013).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from celery import Task

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import session_scope
from app.files import immutability, paths
from app.models_lib import registry, router
from app.core.celery_app import celery_app, register_job_dispatcher
from app.services import job_execution

log = logging.getLogger("ai_studio.tasks.audio")


def _now():
    return datetime.now(timezone.utc)


def _progress(ex, audio_id: str, pct: float, stage: str) -> None:
    """Guarded heartbeat (also a cancel check), then SSE."""
    job_execution.require(ex, {})
    get_bridge().publish(
        CHANNEL,
        "job.progress",
        {
            "job_id": audio_id,
            "job_type": "tts",
            "status": "running",
            "progress_pct": pct,
            "stage": stage,
        },
    )


def _save_wav(row, data: bytes, tag: str) -> str:
    """Save a TTS wav under projects/{project_id}/audio/ (immutable).

    One file per execution attempt (`tag` = exec token prefix): a lost or
    failed attempt leaves an orphan that never blocks the next attempt; the
    token-guarded success UPDATE publishes `output_path` (ADR-0013 B4a-fix).
    """
    rel_dir = f"projects/{row.project_id}/audio"
    filename = f"{row.id.hex}_{tag}.wav"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, data)
    return paths.assert_relative(f"{rel_dir}/{filename}")


def _fit_lipsync(row, rel_path: str, tag: str) -> tuple[str, dict | None, float, str | None]:
    """Optional dubbing lip-sync fit (P1-5 F-13): adjust the speed to the
    requested clip length, ±10% band, warn beyond. Files only (no DB); the
    fitted file is per attempt too (`{id}_{tag}_lipsync.wav`)."""
    target = (row.params or {}).get("target_duration_sec")
    if not target:
        return rel_path, None, 1.0, None
    from app.services import lipsync

    root = paths.projects_root()
    fit_rel = f"projects/{row.project_id}/audio/{row.id.hex}_{tag}_lipsync.wav"
    fit_abs = paths.ensure_parent(root / fit_rel)
    res = lipsync.fit_audio_to_duration(root / rel_path, float(target), fit_abs)
    applied_rate = res["applied_rate"]
    warning = res["warning"]
    if warning is None:
        # Within band: the fitted file becomes the output.
        rel_path = paths.assert_relative(fit_rel)
    else:
        # Beyond band: keep the original, drop the untouched copy.
        fit_abs.unlink(missing_ok=True)
    info = {
        "target_duration_sec": float(target),
        "applied_rate": applied_rate,
        "warning": warning,
    }
    return rel_path, info, applied_rate, warning


@celery_app.task(name="q.audio.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one audio (TTS) generation end to end. Returns a summary dict.

    Same execution-ownership contract as the image worker (ADR-0013).
    """
    from app.models.audio import AudioGeneration

    aid = uuid.UUID(generation_id)
    ex = job_execution.claim(AudioGeneration, aid)
    if ex is None:
        status = job_execution.current_status(AudioGeneration, aid)
        if status is None:
            raise StudioError(
                ErrorCode.NOT_FOUND, "Audio generation not found.",
                details={"job_id": generation_id},
            )
        log.info("audio generation not claimable (status=%s), no-op job_id=%s",
                 status, generation_id)
        return {"generation_id": generation_id, "status": status}

    phase = job_execution.PHASE_PRE
    try:
        with job_execution.keepalive(ex):
            try:
                _progress(ex, generation_id, 5.0, "routing")
                with session_scope() as db:
                    row = db.get(AudioGeneration, aid)
                    routed = router.route(
                        task="tts",
                        modality="tts",
                        explicit_model_id=row.model_id,
                        db=db,
                    )
                    # The adapter runs the DB-overlay config (B7-r2 M4).
                    model = registry.get_model(routed["model_id"], db)
                    db.expunge(row)
                adapter = registry.get_adapter(routed["adapter"])
                ex.model_id = routed["model_id"]

                phase = job_execution.PHASE_SUBMITTING
                # emotion_weights travel in the payload for real adapters to map
                # (F-13 §4.21 capabilities.emotion_mapping); the stub ignores them.
                with registry.pinned_model(model):
                    data = job_execution.submit_poll_fetch(
                        ex, adapter, routed["model_id"],
                        {
                            "text": row.text or "",
                            "voice_id": row.voice_id,
                            "emotion_weights": row.emotion_weights,
                        },
                        progress=lambda pct, stage: _progress(ex, generation_id, pct, stage),
                    )
                phase = job_execution.PHASE_LOCAL
                tag = ex.token.hex[:8]
                job_execution.require(ex, {})
                rel_path = _save_wav(row, data, tag)
                rel_path, lipsync_info, applied_rate, warning = _fit_lipsync(row, rel_path, tag)

                from app.media import ffmpeg_ops

                facts = ffmpeg_ops.probe(paths.projects_root() / rel_path)

                def _apply_params(db, owned) -> None:
                    if lipsync_info is not None:
                        params = dict(owned.params or {})
                        params["lipsync"] = lipsync_info
                        owned.params = params

                done = job_execution.guarded_update(
                    ex,
                    {
                        "status": "done",
                        "output_path": rel_path,
                        "duration_sec": facts.get("duration_sec") or None,
                        "model_id": routed["model_id"],
                        "actual_cost_usd": AudioGeneration.estimated_cost_usd,
                        "error_code": None,
                        "error_message": None,
                    },
                    after=_apply_params,
                )
                if not done:
                    raise job_execution.OwnershipLost(generation_id)
                duration = facts.get("duration_sec") or None
            except (job_execution.OwnershipLost, job_execution.JobFailure):
                raise
            except Exception as exc:  # noqa: BLE001 - persisted as a failure below
                raise job_execution.JobFailure(phase, job_execution.as_studio_error(exc)) from exc
    except job_execution.OwnershipLost:
        log.info("audio generation lost ownership (canceled/superseded) job_id=%s", generation_id)
        return {"generation_id": generation_id, "status": "lost"}
    except job_execution.JobFailure as failure:
        return _handle_failure(self, ex, failure, generation_id)

    get_bridge().publish(
        CHANNEL,
        "job.completed",
        {
            "job_id": generation_id,
            "job_type": "tts",
            "status": "done",
            "output_count": 1,  # contract field (sse_events.json job.completed)
            "output_path": rel_path,
            "duration_sec": duration,
            "lipsync_applied_rate": applied_rate,
            "lipsync_warning": warning,
        },
    )
    log.info("audio generation done job_id=%s model=%s",
             generation_id, routed["model_id"])
    return {
        "generation_id": generation_id,
        "status": "done",
        "output_path": rel_path,
        "lipsync_warning": warning,
    }


def _handle_failure(self: Task, ex, failure, generation_id: str) -> dict:
    """Commit the failure first (retry_count persisted), then SSE / retry / raise."""
    outcome, code = job_execution.record_failure(ex, failure)
    exc = failure.error
    if outcome == "lost":
        log.info("audio failure not recorded (ownership lost) job_id=%s", generation_id)
        return {"generation_id": generation_id, "status": "lost"}
    if outcome == "retry":
        log.warning(
            "audio generation failed, retrying job_id=%s attempt=%s code=%s",
            generation_id, ex.retry_count + 1, code,
        )
        raise self.retry(exc=exc, countdown=job_execution.retry_countdown(ex.retry_count + 1))
    get_bridge().publish(
        CHANNEL,
        "job.failed",
        {
            "job_id": generation_id,
            "job_type": "tts",
            "error_code": code,
            "error_message": exc.message,
            "retryable": False,
            "retry_count": ex.retry_count,
            "max_retries": defaults.RETRY_MAX_ATTEMPTS,
        },
    )
    log.warning("audio generation failed job_id=%s code=%s",
                generation_id, code)
    raise exc


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.audio.run_generation", run_generation.delay)
