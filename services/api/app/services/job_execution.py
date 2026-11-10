"""Job execution ownership: claim, lease/heartbeat, guarded writes (ADR-0013).

Plan D4: a worker owns a job row only through an execution token issued by
one conditional UPDATE (``status='queued' -> 'running'``). Every later write
— progress, heartbeat, submit state, success, failure and the dependent
Shot/Video/parent updates — is a short transaction whose first statement is
``UPDATE ... WHERE id=:id AND exec_token=:token AND status='running'``. Zero
rows means the execution lost ownership (canceled, reconciled, superseded):
the worker stops, publishes nothing, and any file it already wrote stays an
orphan. No DB transaction is held open across provider calls or polling.

Plan D7: ``submit_state='intent'`` is committed BEFORE ``adapter.submit``;
the provider id is committed as ``submitted``. Any failure after intent whose
outcome is unknown becomes ``failed`` + ``PROVIDER_UNCERTAIN`` and is never
resubmitted automatically (no self.retry, no redelivery, no reconcile).

Also owns the job_type -> Celery task registry used by retry and reconcile
(task argument = single target-row id, ADR-0008).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.errors import ErrorCode, StudioError
from app.db.session import dispatch_after_commit, session_scope
from app.models.audio import AudioGeneration
from app.models.generation import Generation, GenerationAttempt
from app.models_lib.adapters.http_polling import poll_until_done
from app.models_lib.base import JobStatus, ProviderAdapter

log = logging.getLogger("ai_studio.services.job_execution")

SUBMIT_NONE = "none"
SUBMIT_INTENT = "intent"
SUBMIT_SUBMITTED = "submitted"

TERMINAL_STATUSES = ("succeeded", "done", "failed", "canceled")

# --- job_type -> task registry (retry / reconcile dispatch) -------------------

IMAGE_JOB_TYPES = (
    "text_to_image",
    "image_to_image",
    "inpaint",
    "outpaint",
    "upscale",
    "thumbnail",
)
VIDEO_JOB_TYPES = ("text_to_video", "image_to_video", "video_extend")
P1_LEDGER_JOB_TYPES = ("lora_train", "style_extract")
AUDIO_TASK = "q.audio.run_generation"


@dataclass(frozen=True)
class JobRoute:
    task_name: str
    # params key holding the target row id; None = the generation row itself.
    target_param: str | None = None


JOB_TASKS: dict[str, JobRoute] = {
    **{t: JobRoute("q.image.run_generation") for t in IMAGE_JOB_TYPES},
    **{t: JobRoute("q.video.run_generation") for t in VIDEO_JOB_TYPES},
    "tts": JobRoute(AUDIO_TASK, "audio_generation_id"),
    "lora_train": JobRoute("q.p1.run_lora_train", "lora_model_id"),
    # The ledger id IS the style_extractions job id (one logical job).
    "style_extract": JobRoute("q.p1.run_style_extract"),
    "multishot": JobRoute("q.p1.run_multishot", "multishot_set_id"),
}


def dispatch_target(gen: Generation) -> tuple[str, str]:
    """(task name, target row id) for a generation row, per its job_type."""
    route = JOB_TASKS.get(gen.job_type or "")
    if route is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "This job type cannot be dispatched.",
            details={"job_type": gen.job_type},
        )
    if route.target_param is None:
        return route.task_name, str(gen.id)
    target = (gen.params or {}).get(route.target_param)
    if not target:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Job has no dispatch target.",
            details={"job_type": gen.job_type, "missing": route.target_param},
        )
    return route.task_name, str(target)


def schedule_dispatch(db: Session, task_name: str, target_id: str) -> None:
    """Send the task after this session commits (rows first, then the broker)."""
    dispatch_after_commit(db, lambda: dispatch_job(task_name, target_id))


def requeue_values(now: datetime | None = None) -> dict[str, Any]:
    """Column values that hand a row back to the queue with no owner."""
    return {
        "status": "queued",
        "exec_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "submit_state": SUBMIT_NONE,
        "dispatched_at": now or _now(),
    }


# --- execution ------------------------------------------------------------------


class OwnershipLost(Exception):
    """The execution token no longer owns the row (0-row guarded write)."""


@dataclass
class Execution:
    model: type
    row_id: uuid.UUID
    token: uuid.UUID
    retry_count: int = 0
    attempt_id: uuid.UUID | None = None
    model_id: str | None = None
    provider_job_id: str | None = None
    lost: threading.Event = field(default_factory=threading.Event)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_until(now: datetime) -> datetime:
    return now + timedelta(seconds=defaults.LEASE_TTL_SEC)


def current_status(model: type, row_id: uuid.UUID) -> str | None:
    with session_scope() as db:
        row = db.get(model, row_id)
        return None if row is None else row.status


def claim(model: type, row_id: uuid.UUID) -> Execution | None:
    """queued -> running with a fresh token, in its own committed transaction.

    Returns None when the row is not queued (terminal, canceled, or another
    execution owns it): the delivery is a no-op and the message is ACKed.
    For generations a GenerationAttempt row is opened in the same transaction.
    """
    now = _now()
    token = uuid.uuid4()
    values: dict[str, Any] = {
        "status": "running",
        "exec_token": token,
        "lease_expires_at": _lease_until(now),
        "heartbeat_at": now,
        "submit_state": SUBMIT_NONE,
    }
    if model is Generation:
        values["started_at"] = func.coalesce(Generation.started_at, now)
    if model is AudioGeneration:
        values["attempt_count"] = AudioGeneration.attempt_count + 1
    with session_scope() as db:
        result = db.execute(
            update(model)
            .where(model.id == row_id, model.status == "queued")
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        row = db.get(model, row_id)
        ex = Execution(
            model=model,
            row_id=row_id,
            token=token,
            retry_count=int(row.retry_count or 0),
            model_id=row.model_id,
        )
        if model is Generation:
            last = (
                db.query(func.max(GenerationAttempt.attempt_no))
                .filter(GenerationAttempt.generation_id == row_id)
                .scalar()
            )
            attempt = GenerationAttempt(
                generation_id=row_id,
                attempt_no=int(last or 0) + 1,
                model_id=row.model_id or "",
                started_at=now,
            )
            db.add(attempt)
            db.flush()
            ex.attempt_id = attempt.id
    return ex


def guarded_update(
    ex: Execution,
    values: dict[str, Any],
    *,
    after: Callable[[Session, Any], None] | None = None,
) -> bool:
    """Conditional UPDATE on id + exec_token + status='running', committed.

    Doubles as a heartbeat (extends the lease while the row stays running).
    ``after(db, row)`` runs in the same transaction only when the UPDATE hit
    the row, so dependent updates are applied exactly by the owner. Returns
    False (and marks the execution lost) when 0 rows matched.
    """
    now = _now()
    vals = dict(values)
    vals.setdefault("heartbeat_at", now)
    if vals.get("status", "running") == "running":
        vals.setdefault("lease_expires_at", _lease_until(now))
    else:
        vals.setdefault("lease_expires_at", None)
    model = ex.model
    with session_scope() as db:
        result = db.execute(
            update(model)
            .where(
                model.id == ex.row_id,
                model.exec_token == ex.token,
                model.status == "running",
            )
            .values(**vals)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            ex.lost.set()
            return False
        if after is not None:
            after(db, db.get(model, ex.row_id))
    return True


def require(ex: Execution, values: dict[str, Any], *, after=None) -> None:
    if ex.lost.is_set() or not guarded_update(ex, values, after=after):
        raise OwnershipLost(str(ex.row_id))


@contextmanager
def keepalive(ex: Execution, interval: float | None = None) -> Iterator[Execution]:
    """Background heartbeat every HEARTBEAT_INTERVAL_SEC while work runs.

    Covers long phases with no progress writes (submit call, download, local
    media work). A 0-row heartbeat marks the execution lost; the worker sees
    it at its next guarded write.
    """
    stop = threading.Event()
    period = float(
        interval if interval is not None else defaults.HEARTBEAT_INTERVAL_SEC
    )

    def _loop() -> None:
        while not stop.wait(period):
            try:
                if not guarded_update(ex, {}):
                    return
            except Exception:  # noqa: BLE001 - heartbeat must never kill the job
                log.exception("heartbeat failed row=%s", ex.row_id)

    thread = threading.Thread(target=_loop, name=f"heartbeat-{ex.row_id}", daemon=True)
    thread.start()
    try:
        yield ex
    finally:
        stop.set()
        thread.join(timeout=5)


def finish_attempt(
    db: Session,
    ex: Execution,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Close this execution's GenerationAttempt row (generations only)."""
    if ex.attempt_id is None:
        return
    attempt = db.get(GenerationAttempt, ex.attempt_id)
    if attempt is None:
        return
    attempt.finished_at = _now()
    if ex.model_id:
        attempt.model_id = ex.model_id
    if ex.provider_job_id:
        attempt.provider_job_id = ex.provider_job_id
    attempt.error_code = error_code
    attempt.error_message = error_message


# --- provider call with D7 submit intent ---------------------------------------

# Phases of one execution, used to classify a failure (D7).
PHASE_PRE = "pre"  # before submit intent: normal retry policy
PHASE_SUBMITTING = "submitting"  # intent committed, submit call in flight
PHASE_POLLING = "polling"  # provider id known, outcome not final
PHASE_PROVIDER_FAILED = "provider_failed"  # provider reported a final failure
PHASE_FETCHING = "fetching"  # provider succeeded, download failed
PHASE_LOCAL = "local"  # result in hand, local save/bookkeeping failed

# httpx errors raised before any request byte reached the provider.
_PRE_SEND_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
)
# Definite provider/config rejections: the job was certainly not accepted.
_DEFINITE_REJECTIONS = {
    ErrorCode.RATE_LIMITED,
    ErrorCode.MODEL_UNAVAILABLE,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.MODERATION_BLOCKED,
    ErrorCode.PROMPT_TOO_LONG,
    ErrorCode.BUDGET_EXCEEDED,
}


class JobFailure(Exception):
    def __init__(self, phase: str, error: StudioError) -> None:
        super().__init__(error.message)
        self.phase = phase
        self.error = error


def _root_cause(exc: BaseException) -> BaseException | None:
    seen = set()
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, httpx.HTTPError):
            return cause
        cause = cause.__cause__ or cause.__context__
    return None


def submit_not_sent(exc: StudioError) -> bool:
    """True only when the submit certainly did not create a provider job."""
    cause = _root_cause(exc)
    if isinstance(cause, _PRE_SEND_ERRORS):
        return True
    if cause is None and exc.code in _DEFINITE_REJECTIONS:
        return True
    return False


def as_studio_error(exc: BaseException) -> StudioError:
    if isinstance(exc, StudioError):
        return exc
    err = StudioError(
        ErrorCode.PROVIDER_ERROR,
        f"Worker error ({exc.__class__.__name__}).",
        retryable=False,
    )
    err.__cause__ = exc
    return err


def classify(failure: JobFailure) -> tuple[str, bool]:
    """(error_code to persist, automatic retry allowed) for a failure (D7)."""
    exc = failure.error
    if failure.phase == PHASE_PRE:
        return exc.code, exc.retryable
    if failure.phase == PHASE_SUBMITTING:
        if submit_not_sent(exc):
            return exc.code, exc.retryable
        return ErrorCode.PROVIDER_UNCERTAIN, False
    if failure.phase in (PHASE_POLLING, PHASE_FETCHING):
        return ErrorCode.PROVIDER_UNCERTAIN, False
    # provider_failed / local: outcome known, but a provider job existed, so
    # never auto-resubmit (only submit_state='none' may be auto-submitted).
    return exc.code, False


def best_effort_cancel(adapter: ProviderAdapter, provider_job_id: str | None) -> None:
    if not provider_job_id:
        return
    try:
        adapter.cancel(provider_job_id)
    except Exception:  # noqa: BLE001 - best effort by contract
        log.warning("provider cancel failed provider_job_id=%s", provider_job_id)


def submit_poll_fetch(
    ex: Execution,
    adapter: ProviderAdapter,
    model_id: str,
    payload: dict,
    *,
    progress: Callable[[float, str], None],
) -> bytes:
    """Run the provider protocol under D7. Returns the artifact bytes.

    Raises JobFailure(phase, error) on failure and OwnershipLost when the
    execution was canceled/superseded (the provider job is then canceled
    best-effort).
    """
    require(ex, {"submit_state": SUBMIT_INTENT})
    try:
        provider_job_id = adapter.submit(model_id, payload)
    except Exception as exc:  # noqa: BLE001 - classified below
        raise JobFailure(PHASE_SUBMITTING, as_studio_error(exc)) from exc
    ex.provider_job_id = provider_job_id

    def _record_pid(db: Session, row: Any) -> None:
        if ex.attempt_id is not None:
            attempt = db.get(GenerationAttempt, ex.attempt_id)
            if attempt is not None:
                attempt.provider_job_id = provider_job_id
                attempt.model_id = model_id

    if not guarded_update(
        ex,
        {"submit_state": SUBMIT_SUBMITTED, "provider_job_id": provider_job_id},
        after=_record_pid,
    ):
        best_effort_cancel(adapter, provider_job_id)
        raise OwnershipLost(str(ex.row_id))

    try:
        progress(20.0, "submitted")

        def _on_poll(status: JobStatus) -> None:
            progress(20.0 + status.progress_pct * 0.5, status.stage or "polling")

        try:
            final = poll_until_done(adapter, provider_job_id, on_progress=_on_poll)
        except (OwnershipLost, JobFailure):
            raise
        except Exception as exc:  # noqa: BLE001
            raise JobFailure(PHASE_POLLING, as_studio_error(exc)) from exc
        if final.state != "succeeded":
            code = (
                final.error_code
                if final.error_code in ErrorCode.ALL
                else ErrorCode.PROVIDER_ERROR
            )
            raise JobFailure(
                PHASE_PROVIDER_FAILED,
                StudioError(code, final.error_message or "Provider reported failure."),
            )
        progress(90.0, "saving")
        try:
            return adapter.fetch_result(provider_job_id)
        except Exception as exc:  # noqa: BLE001
            raise JobFailure(PHASE_FETCHING, as_studio_error(exc)) from exc
    except OwnershipLost:
        best_effort_cancel(adapter, provider_job_id)
        raise


def record_failure(
    ex: Execution,
    failure: JobFailure,
    *,
    terminal_values: dict[str, Any] | None = None,
    after_fail: Callable[[Session, Any, str, str], None] | None = None,
) -> tuple[str, str]:
    """Persist a failure (committed BEFORE any raise / SSE).

    Returns (outcome, error_code) with outcome in retry | failed | lost:
      retry  -> row is back to queued (exec fields cleared, retry_count + 1);
                the caller raises self.retry.
      failed -> row is failed; caller publishes job.failed, then raises.
      lost   -> the execution no longer owns the row; caller stops silently.
    """
    code, auto = classify(failure)
    message = failure.error.message
    model = ex.model
    if auto and ex.retry_count < defaults.RETRY_MAX_ATTEMPTS:
        values = requeue_values()
        values.update(
            {
                "retry_count": model.retry_count + 1,
                "error_code": code,
                "error_message": message,
                "provider_job_id": None,
            }
        )

        def _after_retry(db: Session, row: Any) -> None:
            finish_attempt(db, ex, error_code=code, error_message=message)

        if guarded_update(ex, values, after=_after_retry):
            return "retry", code
        return "lost", code

    values = {"status": "failed", "error_code": code, "error_message": message}
    if model is Generation:
        values["finished_at"] = _now()
    if terminal_values:
        values.update(terminal_values)

    def _after_fail(db: Session, row: Any) -> None:
        finish_attempt(db, ex, error_code=code, error_message=message)
        if after_fail is not None:
            after_fail(db, row, code, message)

    if guarded_update(ex, values, after=_after_fail):
        return "failed", code
    return "lost", code


def retry_countdown(retry_count: int) -> float:
    import random

    return defaults.RETRY_BACKOFF_BASE_SEC * (
        2 ** max(retry_count - 1, 0)
    ) + random.uniform(0, defaults.RETRY_JITTER_SEC)


# --- terminal side effects on generations (cancel / reconcile / workers) ------


def on_generation_terminal(db: Session, gen: Generation, *, success: bool) -> None:
    """Recompute rows that depend on a generation reaching a terminal state.

    Image jobs: batch parent aggregate, frame shot status, multishot frame.
    Video jobs: the Video row (when not done) and the shot's video status.
    LoRA ledger: a still-training LoRA fails with its ledger.
    """
    job_type = gen.job_type or ""
    if job_type in VIDEO_JOB_TYPES:
        from app.services import videos as video_service

        if not success:
            video_service.mark_video_failed(db, gen)
        if gen.shot_id is not None:
            video_service.recompute_shot_video_status(db, gen.shot_id)
        return
    if job_type == "style_extract":
        if not success:
            from app.services import style_extract as style_service

            style_service.on_ledger_terminal(db, gen)
        return
    if job_type == "lora_train" and not success:
        from app.models.lora import LoraModel

        lora_id = (gen.params or {}).get("lora_model_id")
        lora = db.get(LoraModel, uuid.UUID(str(lora_id))) if lora_id else None
        if lora is not None and lora.status == "training":
            lora.status = "failed"
        return
    if job_type in IMAGE_JOB_TYPES:
        from app.services import generations as generation_service

        generation_service.aggregate_batch_parent(db, gen)
        if gen.shot_id is not None and not gen.is_batch:
            generation_service.recompute_frame_shot_status(db, gen.shot_id)
        frame_id = (gen.params or {}).get("_multishot_frame_id")
        if frame_id:
            try:
                from app.services import multishot as multishot_service

                multishot_service.on_frame_finished(db, str(frame_id), success)
            except StudioError:
                log.warning("multishot aggregation skipped for frame %s", frame_id)
    db.flush()
