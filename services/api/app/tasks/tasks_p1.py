"""P1 async jobs (q.p1.*, routed onto the q.post queue).

Thin wrappers by design (ARCHITECTURE §8): each task takes exactly one
argument — the target row id (ADR-0008) — opens a session, and delegates to
the owning service. tasks -> services is allowed; services -> tasks is not,
so services enqueue via core.celery_app.dispatch_job.
"""
from __future__ import annotations

import logging

from celery import Task

from app.core import defaults
from app.core.celery_app import celery_app, register_job_dispatcher
from app.db.session import session_scope

log = logging.getLogger("ai_studio.tasks.p1")


@celery_app.task(name="q.p1.run_multishot", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_multishot(self: Task, multishot_set_id: str) -> dict:
    """Insert the 6 angle shots and queue their frame jobs (P1-1)."""
    from app.services import multishot as multishot_service

    try:
        with session_scope() as db:
            summary = multishot_service.run_multishot(db, multishot_set_id)
        log.info("multishot queued multishot_set_id=%s", multishot_set_id)
        return summary
    except Exception:
        log.exception("multishot crashed multishot_set_id=%s", multishot_set_id)
        raise


register_job_dispatcher("q.p1.run_multishot", run_multishot.delay)


@celery_app.task(name="q.p1.run_lora_train", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_lora_train(self: Task, lora_model_id: str) -> dict:
    """Train a LoRA for a character (P1-4). Stub completes immediately."""
    from app.core.errors import StudioError
    from app.services import lora as lora_service

    try:
        with session_scope() as db:
            summary = lora_service.complete_train(db, lora_model_id)
        log.info("lora train finished lora_model_id=%s", lora_model_id)
        return summary
    except StudioError:
        raise
    except Exception as exc:
        log.exception("lora train crashed lora_model_id=%s", lora_model_id)
        with session_scope() as db:
            lora_service.fail_train(db, lora_model_id, "PROVIDER_ERROR", str(exc))
        raise


register_job_dispatcher("q.p1.run_lora_train", run_lora_train.delay)


@celery_app.task(name="q.p1.run_style_extract", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_style_extract(self: Task, style_kit_id: str) -> dict:
    """Extract style fields from source images into a style kit (P1-7)."""
    from app.services import style_extract as style_service

    try:
        with session_scope() as db:
            summary = style_service.complete_extract(db, style_kit_id)
        log.info("style extract finished style_kit_id=%s", style_kit_id)
        return summary
    except Exception:
        log.exception("style extract crashed style_kit_id=%s", style_kit_id)
        raise


register_job_dispatcher("q.p1.run_style_extract", run_style_extract.delay)
