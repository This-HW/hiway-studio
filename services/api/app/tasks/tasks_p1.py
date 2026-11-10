"""P1 async jobs (q.p1.*, routed onto the q.post queue).

Thin wrappers by design (ARCHITECTURE §8): each task takes exactly one
argument — the target row id (ADR-0008) — opens a session, and delegates to
the owning service. tasks -> services is allowed; services -> tasks is not,
so services enqueue via core.celery_app.dispatch_job.
"""
from __future__ import annotations

import logging
import uuid

from celery import Task
from celery.signals import worker_ready
from sqlalchemy import update

from app.core import defaults
from app.core.celery_app import celery_app, register_job_dispatcher
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import session_scope

log = logging.getLogger("ai_studio.tasks.p1")


@celery_app.task(name="q.p1.run_multishot", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_multishot(self: Task, multishot_set_id: str) -> dict:
    """Insert the 6 angle shots and queue their frame jobs (P1-1).

    Single fan-out (plan D9, RH2): the service claims the set and creates all
    children in this one transaction; a duplicate delivery is a no-op and the
    children are dispatched only after the commit. A crash rolls everything
    back (set still queued, no children) and is then committed as failed.
    """
    from app.services import multishot as multishot_service

    try:
        with session_scope() as db:
            summary = multishot_service.run_multishot(db, multishot_set_id)
        log.info("multishot fan-out multishot_set_id=%s status=%s",
                 multishot_set_id, summary.get("status"))
        return summary
    except Exception as exc:
        log.exception("multishot crashed multishot_set_id=%s", multishot_set_id)
        code = getattr(exc, "code", "PROVIDER_ERROR")
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        try:
            multishot_service.fail_set(multishot_set_id, code, message)
        except Exception:  # noqa: BLE001 - keep the original error
            log.exception("multishot failure not recorded multishot_set_id=%s",
                          multishot_set_id)
        raise


register_job_dispatcher("q.p1.run_multishot", run_multishot.delay)


def _lock_if(db, model, row_id: uuid.UUID, status: str) -> bool:
    """Stable job guard: conditional no-op UPDATE that row-locks the target
    while it is still in ``status``. A concurrent/redelivered duplicate then
    blocks and re-checks (0 rows once the first execution committed), so the
    side effect below runs once per logical job (plan D9)."""
    result = db.execute(
        update(model)
        .where(model.id == row_id, model.status == status)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _fail_ledger(db, ledger_id, code: str, message: str) -> None:
    """Persist the failure on the cost-ledger generation (terminal, committed)."""
    from datetime import datetime, timezone

    from app.models.generation import Generation

    if ledger_id is None:
        return
    db.execute(
        update(Generation)
        .where(Generation.id == ledger_id, Generation.status.in_(("queued", "running")))
        .values(status="failed", error_code=code, error_message=message[:500],
                finished_at=datetime.now(timezone.utc), exec_token=None,
                lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )


@celery_app.task(name="q.p1.run_lora_train", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_lora_train(self: Task, lora_model_id: str) -> dict:
    """Train a LoRA for a character (P1-4). Stub completes immediately.

    Stable guard: runs only while the LoRA is 'training' (and its ledger is
    still queued); a redelivery of a finished/failed/canceled job is a no-op.
    Any failure is committed (LoRA + ledger failed) before re-raising.
    """
    from app.models.generation import Generation
    from app.models.lora import LoraModel
    from app.services import lora as lora_service

    lid = uuid.UUID(lora_model_id)
    try:
        with session_scope() as db:
            lora = db.get(LoraModel, lid)
            if lora is None or not _lock_if(db, LoraModel, lid, "training"):
                status = lora.status if lora is not None else "missing"
                log.info("lora train not runnable (status=%s), no-op lora_model_id=%s",
                         status, lora_model_id)
                return {"lora_model_id": lora_model_id, "status": status, "resumed": True}
            if lora.generation_id is not None and not _lock_if(
                db, Generation, lora.generation_id, "queued"
            ):
                log.info("lora ledger not queued, no-op lora_model_id=%s", lora_model_id)
                return {"lora_model_id": lora_model_id, "status": lora.status, "resumed": True}
            summary = lora_service.complete_train(db, lora_model_id)
        log.info("lora train finished lora_model_id=%s", lora_model_id)
        return summary
    except Exception as exc:
        log.exception("lora train crashed lora_model_id=%s", lora_model_id)
        code = getattr(exc, "code", "PROVIDER_ERROR")
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        with session_scope() as db:
            lora = db.get(LoraModel, lid)
            ledger_id = lora.generation_id if lora is not None else None
            lora_service.fail_train(db, lora_model_id, code, message)
            _fail_ledger(db, ledger_id, code, message)
        raise


register_job_dispatcher("q.p1.run_lora_train", run_lora_train.delay)


@celery_app.task(name="q.p1.run_style_extract", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_style_extract(self: Task, style_extraction_id: str) -> dict:
    """Extract style fields from source images into a style kit (P1-7).

    The argument is the extraction JOB id (style_extractions.id; for a
    project kit also its cost-ledger generation id) — ADR-0008, plan D9.
    The service claims exactly that row and completes it in one
    transaction; a terminal job (done / failed / canceled) is a no-op, so a
    redelivered message never completes another extraction of the same kit
    and a global kit's message applies once. A crash is committed as failed
    (job row + ledger) before re-raising.
    """
    from app.services import style_extract as style_service

    try:
        with session_scope() as db:
            summary = style_service.complete_extract(db, style_extraction_id)
        if summary.get("noop"):
            log.info("style extract not runnable (status=%s), no-op job_id=%s",
                     summary.get("status"), style_extraction_id)
        else:
            log.info("style extract finished job_id=%s", style_extraction_id)
        return summary
    except Exception as exc:
        log.exception("style extract crashed job_id=%s", style_extraction_id)
        code = getattr(exc, "code", "PROVIDER_ERROR")
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        recorded = False
        try:
            with session_scope() as db:
                recorded = style_service.fail_extract(db, style_extraction_id, code, message)
        except Exception:  # noqa: BLE001 - keep the original error
            log.exception("style extract failure not recorded job_id=%s",
                          style_extraction_id)
        if recorded:
            get_bridge().publish(CHANNEL, "job.failed", {
                "job_id": style_extraction_id, "job_type": "style_extract",
                "status": "failed", "error_code": code, "error_message": message,
                "retryable": False, "retry_count": 0, "max_retries": 0,
            })
        raise


register_job_dispatcher("q.p1.run_style_extract", run_style_extract.delay)


# --- reconcile backup (ADR-0013) ------------------------------------------------
# The API lifespan loop is the primary reconcile driver; a worker that comes
# up runs one pass too, so jobs lost while the API was down are recovered.


@worker_ready.connect
def _reconcile_on_worker_ready(**_kwargs) -> None:
    from app.services import job_recovery

    try:
        job_recovery.reconcile_once()
    except Exception:  # noqa: BLE001 - never block worker start-up
        log.exception("reconcile on worker_ready failed")
