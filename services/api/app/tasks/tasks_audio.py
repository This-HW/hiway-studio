"""Audio queue worker (q.audio, concurrency 2).

Task signature is fixed: run_generation(generation_id: str) — single argument,
the *audio* generation id (ADR-0008: one target row id + DB as source of
truth). Prompt text, voice id and emotion weights are never passed as task
arguments.

Pipeline: route -> submit -> poll -> fetch_result -> save wav ->
(optional lipsync fit) -> update row -> SSE events.

Retry policy (§12.2): retryable failures are retried up to
defaults.RETRY_MAX_ATTEMPTS with exponential backoff; the retry count lives
in the row's params JSON (audio_generations has no attempts table).
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
from app.files import immutability, paths
from app.models_lib import registry, router
from app.models_lib.adapters.http_polling import poll_until_done
from app.models_lib.base import JobStatus
from app.core.celery_app import celery_app, register_job_dispatcher

log = logging.getLogger("ai_studio.tasks.audio")


def _now():
    return datetime.now(timezone.utc)


def _progress(db, row, audio_id: str, pct: float, stage: str) -> None:
    db.flush()
    get_bridge().publish(
        CHANNEL,
        "job.progress",
        {
            "job_id": audio_id,
            "job_type": "tts",
            "status": row.status,
            "progress_pct": pct,
            "stage": stage,
        },
    )


def _save_wav(db, row, data: bytes, suffix: str = "") -> str:
    """Save a TTS wav under projects/{project_id}/audio/ (immutable)."""
    rel_dir = f"projects/{row.project_id}/audio"
    filename = f"{row.id.hex}{suffix}.wav"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_dir / filename)
    immutability.write_new(abs_path, data)
    return paths.assert_relative(f"{rel_dir}/{filename}")


def _retry_count(row) -> int:
    return int((row.params or {}).get("_retry_count", 0) or 0)


def _set_retry_count(db, row, n: int) -> None:
    params = dict(row.params or {})
    params["_retry_count"] = n
    row.params = params
    db.flush()


@celery_app.task(name="q.audio.run_generation", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_generation(self: Task, generation_id: str) -> dict:
    """Run one audio (TTS) generation end to end. Returns a summary dict."""
    from app.models.audio import AudioGeneration

    with session_scope() as db:
        row = db.get(AudioGeneration, uuid.UUID(generation_id))
        if row is None:
            raise StudioError(
                ErrorCode.NOT_FOUND, "Audio generation not found.",
                details={"job_id": generation_id},
            )

        # Canceled while queued: never run the provider call.
        if row.status == "canceled":
            log.info("audio generation canceled before run job_id=%s", generation_id)
            return {"generation_id": generation_id, "status": "canceled"}

        row.status = "running"
        db.flush()

        try:
            _progress(db, row, generation_id, 5.0, "routing")
            routed = router.route(
                task="tts",
                modality="tts",
                explicit_model_id=row.model_id,
                db=db,
            )
            adapter = registry.get_adapter(routed["adapter"])

            _progress(db, row, generation_id, 20.0, "submitted")
            # emotion_weights travel in the payload for real adapters to map
            # (F-13 §4.21 capabilities.emotion_mapping); the stub ignores them.
            provider_job_id = adapter.submit(routed["model_id"], {
                "text": row.text or "",
                "voice_id": row.voice_id,
                "emotion_weights": row.emotion_weights,
            })

            def _on_poll(status: JobStatus) -> None:
                _progress(
                    db, row, generation_id,
                    20.0 + status.progress_pct * 0.5, status.stage or "polling",
                )

            final = poll_until_done(adapter, provider_job_id, on_progress=_on_poll)
            if final.state != "succeeded":
                raise StudioError(
                    final.error_code or ErrorCode.PROVIDER_ERROR,
                    final.error_message or "Provider reported failure.",
                )

            _progress(db, row, generation_id, 90.0, "saving")
            data = adapter.fetch_result(provider_job_id)
            rel_path = _save_wav(db, row, data)

            # Optional dubbing lip-sync fit (P1-5 F-13): adjust the speed to
            # the requested clip length, ±10% band, warn beyond.
            warning = None
            applied_rate = 1.0
            target = (row.params or {}).get("target_duration_sec")
            if target:
                from app.services import lipsync

                root = paths.projects_root()
                fit_rel = f"projects/{row.project_id}/audio/{row.id.hex}_lipsync.wav"
                fit_abs = paths.ensure_parent(root / fit_rel)
                res = lipsync.fit_audio_to_duration(
                    root / rel_path, float(target), fit_abs)
                applied_rate = res["applied_rate"]
                warning = res["warning"]
                if warning is None:
                    # Within band: the fitted file becomes the output.
                    rel_path = paths.assert_relative(fit_rel)
                else:
                    # Beyond band: keep the original, drop the untouched copy.
                    fit_abs.unlink(missing_ok=True)
                params = dict(row.params or {})
                params["lipsync"] = {
                    "target_duration_sec": float(target),
                    "applied_rate": applied_rate,
                    "warning": warning,
                }
                row.params = params

            from app.media import ffmpeg_ops

            facts = ffmpeg_ops.probe(paths.projects_root() / rel_path)

            row.status = "done"
            row.output_path = rel_path
            row.duration_sec = facts.get("duration_sec") or None
            row.model_id = routed["model_id"]
            row.actual_cost_usd = row.estimated_cost_usd
            row.error_code = None
            row.error_message = None
            db.flush()

            get_bridge().publish(
                CHANNEL,
                "job.completed",
                {
                    "job_id": generation_id,
                    "job_type": "tts",
                    "status": "done",
                    "output_path": rel_path,
                    "duration_sec": row.duration_sec,
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

        except StudioError as exc:
            if exc.retryable and _retry_count(row) < defaults.RETRY_MAX_ATTEMPTS:
                _set_retry_count(db, row, _retry_count(row) + 1)
                row.status = "queued"
                row.error_code = exc.code
                row.error_message = exc.message
                db.flush()
                countdown = (
                    defaults.RETRY_BACKOFF_BASE_SEC * (2 ** (_retry_count(row) - 1))
                    + random.uniform(0, defaults.RETRY_JITTER_SEC)
                )
                log.warning(
                    "audio generation failed, retrying job_id=%s attempt=%s code=%s",
                    generation_id, _retry_count(row), exc.code,
                )
                raise self.retry(exc=exc, countdown=countdown)
            row.status = "failed"
            row.error_code = exc.code
            row.error_message = exc.message
            db.flush()

            get_bridge().publish(
                CHANNEL,
                "job.failed",
                {
                    "job_id": generation_id,
                    "job_type": "tts",
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "retryable": exc.retryable,
                    "retry_count": _retry_count(row),
                    "max_retries": defaults.RETRY_MAX_ATTEMPTS,
                },
            )
            log.warning("audio generation failed job_id=%s code=%s",
                        generation_id, exc.code)
            raise


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.audio.run_generation", run_generation.delay)
