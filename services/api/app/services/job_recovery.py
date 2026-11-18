"""Job reconcile: recover what the broker or a dead worker dropped (H1, ADR-0013).

``reconcile_once(now)`` is idempotent and safe to run concurrently with
workers and with itself — every transition is a conditional UPDATE, and a
duplicate dispatch is harmless because the worker's claim admits one owner.

  (a) ``running`` rows whose lease expired (worker died / lost heartbeat):
      submit_state intent|submitted -> ``failed`` + PROVIDER_UNCERTAIN, never
      re-dispatched (D7); submit_state none -> back to ``queued`` (token
      cleared, retry_count + 1) and re-dispatched, or ``failed`` once the
      automatic retry budget is spent.
  (b) ``queued`` rows not (re)sent for RECONCILE_REDISPATCH_AFTER_SEC ->
      re-dispatched once per window (dispatched_at is the clock).
  (c) P1 ledger rows (lora_train / style_extract) still queued after
      RECONCILE_P1_STUCK_AFTER_SEC -> ``failed`` (their LoRA fails too).

  (d) timeline renders (plan D8): ``rendering`` + started (heartbeat_at set)
      + lease expired -> ``failed`` (local work, no provider cost, nothing
      uncertain); requested but never started + lease expired -> render task
      re-sent once per lease window (the start mark admits one execution);
      legacy ``rendering`` rows without an exec_token -> ``failed``.
  (e) multishot sets (plan D9): ``queued`` sets not sent for
      RECONCILE_REDISPATCH_AFTER_SEC -> q.p1.run_multishot re-sent (its claim
      fans out at most once); ``running`` sets -> terminal children whose
      per-frame hook was lost are reflected into their frames and the set is
      ALWAYS re-aggregated from all frames under its row lock (B4B-H5: also
      when every frame is done but the set stayed running). Reconcile never
      creates coverage shots or generations itself.

  (f) legacy executions (B4A-M1): ``running`` rows with neither exec_token
      nor lease (left running before migration 0012) whose last sign of
      life (heartbeat_at / started_at / updated_at; audio: created_at) is
      older than LEASE_TTL_SEC -> ``failed`` + PROVIDER_UNCERTAIN. Their
      submit state is unknown, so they are never re-dispatched (D7); batch
      parents are excluded (they are aggregated by (g)).
  (g) dependent aggregates (B4A-H3, R2-H1): non-terminal batch parents
      whose children are all terminal are re-aggregated; shots still
      ``frame_generating`` / ``video_generating`` with no pending job are
      recomputed, and — the other direction — settled shots with a pending
      image take (``draft|frame_done|frame_approved|failed``) go back to
      ``frame_generating`` and shots with a pending video job (not already
      ``video_generating``/``frame_generating``) to ``video_generating``.
      The recovery for a lost or racing aggregation; every repair locks the
      shot and re-checks the jobs. Lock order parent -> shot (ADR-0013).
  (h) style extractions without a cost ledger (global kits, R2-M2): a
      ``queued`` row older than RECONCILE_REDISPATCH_AFTER_SEC is re-sent
      on every pass (the row has no dispatched_at; the worker's claim admits
      one run, so a duplicate is a no-op); one still ``queued`` after
      RECONCILE_P1_STUCK_AFTER_SEC, or left ``running`` (committed claim
      without completion) for longer than LEASE_TTL_SEC since it was
      created -> ``failed``. Project-kit rows are recovered through their
      ledger by (b)/(c) and the ledger's terminal hook.

Callers: the API lifespan loop every RECONCILE_INTERVAL_SEC (main.py) and
the worker_ready signal once (tasks_p1.py). Batch parents are never touched
by (a)/(f).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update

from app.core import defaults
from app.core.errors import ErrorCode
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import session_scope
from app.models.audio import AudioGeneration
from app.models.generation import Generation, GenerationAttempt
from app.models.multishot import MultishotSet
from app.models.timeline import Timeline
from app.services import job_execution

log = logging.getLogger("ai_studio.services.job_recovery")

_UNCERTAIN_MESSAGE = (
    "Worker lost after the provider submit intent; the provider may have "
    "accepted the job. Not resubmitted automatically — retry manually."
)
_LOST_MESSAGE = "Worker lost before submitting (execution lease expired)."
_STUCK_MESSAGE = "Job was never picked up by a worker."
_LEGACY_MESSAGE = (
    "Job was left running from before execution tracking; the provider may "
    "have accepted it. Not resubmitted automatically — retry manually."
)
_RENDER_LOST_MESSAGE = (
    "Render worker lost (execution lease expired); request the render again."
)
_RENDER_LEGACY_MESSAGE = (
    "Render left running from before execution tracking; request the render again."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_type(model: type, row: Any) -> str:
    return "tts" if model is AudioGeneration else (row.job_type or "")


def _publish_failed(model: type, row: Any, code: str, message: str) -> None:
    try:
        get_bridge().publish(
            CHANNEL,
            "job.failed",
            {
                "job_id": str(row.id),
                "job_type": _job_type(model, row),
                "error_code": code,
                "error_message": message,
                "retryable": False,
                "retry_count": int(row.retry_count or 0),
                "max_retries": defaults.RETRY_MAX_ATTEMPTS,
            },
        )
    except Exception:  # noqa: BLE001 - SSE is best effort; the row is committed
        log.warning("job.failed publish skipped job_id=%s", row.id)


def _close_open_attempt(db, gen_id: uuid.UUID, code: str, message: str) -> None:
    attempt = (
        db.query(GenerationAttempt)
        .filter(
            GenerationAttempt.generation_id == gen_id,
            GenerationAttempt.finished_at.is_(None),
        )
        .order_by(GenerationAttempt.attempt_no.desc())
        .first()
    )
    if attempt is not None:
        attempt.finished_at = _now()
        attempt.error_code = code
        attempt.error_message = message


def _target(model: type, row: Any) -> tuple[str, str] | None:
    if model is AudioGeneration:
        return job_execution.AUDIO_TASK, str(row.id)
    try:
        return job_execution.dispatch_target(row)
    except Exception:  # noqa: BLE001 - unknown job types are left alone
        return None


def _fail_row(db, model: type, row_id: uuid.UUID, where: list, code: str, message: str):
    values: dict[str, Any] = {
        "status": "failed",
        "error_code": code,
        "error_message": message,
        "lease_expires_at": None,
    }
    if model is Generation:
        values["finished_at"] = _now()
    result = db.execute(
        update(model)
        .where(model.id == row_id, *where)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    row = db.get(model, row_id)
    if model is Generation:
        _close_open_attempt(db, row_id, code, message)
        job_execution.on_generation_terminal(db, row, success=False)
    return row


def _expired_running(model: type, now: datetime, summary: dict[str, int]) -> None:
    cols = [model.id, model.exec_token, model.submit_state, model.retry_count]
    query = select(*cols).where(
        model.status == "running",
        model.lease_expires_at.is_not(None),
        model.lease_expires_at < now,
    )
    if model is Generation:
        query = query.where(Generation.is_batch.is_(False))
    with session_scope() as db:
        candidates = db.execute(query).all()

    for row_id, token, submit_state, retry_count in candidates:
        owner = [
            model.status == "running",
            model.exec_token == token,
            model.lease_expires_at < now,
        ]
        if submit_state in (
            job_execution.SUBMIT_INTENT,
            job_execution.SUBMIT_SUBMITTED,
        ):
            code, message = ErrorCode.PROVIDER_UNCERTAIN, _UNCERTAIN_MESSAGE
            with session_scope() as db:
                row = _fail_row(db, model, row_id, owner, code, message)
            if row is not None:
                summary["uncertain"] += 1
                _publish_failed(model, row, code, message)
            continue

        if int(retry_count or 0) >= defaults.RETRY_MAX_ATTEMPTS:
            code, message = ErrorCode.PROVIDER_ERROR, _LOST_MESSAGE
            with session_scope() as db:
                row = _fail_row(db, model, row_id, owner, code, message)
            if row is not None:
                summary["failed"] += 1
                _publish_failed(model, row, code, message)
            continue

        with session_scope() as db:
            values = job_execution.requeue_values(now)
            values.update(
                retry_count=model.retry_count + 1,
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=_LOST_MESSAGE,
            )
            result = db.execute(
                update(model)
                .where(model.id == row_id, *owner)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
            row = db.get(model, row_id)
            if model is Generation:
                _close_open_attempt(db, row_id, ErrorCode.PROVIDER_ERROR, _LOST_MESSAGE)
            target = _target(model, row)
            if target is not None:
                job_execution.schedule_dispatch(db, *target)
        summary["requeued"] += 1


def _legacy_running(model: type, now: datetime, summary: dict[str, int]) -> None:
    """(f) running rows without token/lease from before migration 0012."""
    cutoff = now - timedelta(seconds=defaults.LEASE_TTL_SEC)
    if model is Generation:
        seen = func.coalesce(
            Generation.heartbeat_at, Generation.started_at, Generation.updated_at
        )
    else:
        seen = func.coalesce(AudioGeneration.heartbeat_at, AudioGeneration.created_at)
    where = [
        model.status == "running",
        model.exec_token.is_(None),
        model.lease_expires_at.is_(None),
        seen < cutoff,
    ]
    if model is Generation:
        where.append(Generation.is_batch.is_(False))
    with session_scope() as db:
        ids = db.execute(select(model.id).where(*where)).scalars().all()
    for row_id in ids:
        code = ErrorCode.PROVIDER_UNCERTAIN
        with session_scope() as db:
            row = _fail_row(db, model, row_id, where, code, _LEGACY_MESSAGE)
        if row is not None:
            summary["uncertain"] += 1
            _publish_failed(model, row, code, _LEGACY_MESSAGE)


def _aggregates(summary: dict[str, int]) -> None:
    """(g) converge batch parents / shots whose children are all terminal."""
    from app.models.production import Shot
    from app.services import generations as generation_service
    from app.services import videos as video_service

    pending = ("queued", "running")
    child = Generation.__table__.alias("child")
    with session_scope() as db:
        parents = (
            db.execute(
                select(Generation.id).where(
                    Generation.is_batch.is_(True),
                    Generation.status.in_(pending),
                    exists().where(child.c.parent_generation_id == Generation.id),
                    ~exists().where(
                        child.c.parent_generation_id == Generation.id,
                        child.c.status.in_(pending),
                    ),
                )
            )
            .scalars()
            .all()
        )
    for parent_id in parents:
        with session_scope() as db:
            if generation_service.aggregate_parent(db, parent_id):
                summary["batch_parents_aggregated"] += 1

    def _idle_shots(status: str, job_filter) -> list:
        with session_scope() as db:
            return (
                db.execute(
                    select(Shot.id).where(
                        Shot.status == status,
                        ~exists().where(
                            Generation.shot_id == Shot.id,
                            Generation.status.in_(pending),
                            job_filter,
                        ),
                    )
                )
                .scalars()
                .all()
            )

    frame_shots = _idle_shots("frame_generating", Generation.is_batch.is_(False))
    video_shots = _idle_shots(
        "video_generating", Generation.job_type.in_(job_execution.VIDEO_JOB_TYPES)
    )
    for shot_id in frame_shots:
        with session_scope() as db:
            if generation_service.recompute_frame_shot_status(db, shot_id):
                summary["shots_recomputed"] += 1
    for shot_id in video_shots:
        with session_scope() as db:
            if video_service.recompute_shot_video_status(db, shot_id):
                summary["shots_recomputed"] += 1

    # R2-H1, other direction: a settled shot with a pending job (a pending
    # transition raced a terminal recompute). The helpers lock the shot and
    # re-check the jobs before writing.
    def _busy_shots(status_filter, job_filter) -> list:
        with session_scope() as db:
            return (
                db.execute(
                    select(Shot.id).where(
                        status_filter,
                        exists().where(
                            Generation.shot_id == Shot.id,
                            Generation.status.in_(pending),
                            job_filter,
                        ),
                    )
                )
                .scalars()
                .all()
            )

    busy_frame = _busy_shots(
        Shot.status.in_(generation_service.FRAME_PENDING_FROM),
        and_(
            Generation.is_batch.is_(False),
            Generation.job_type.in_(job_execution.IMAGE_JOB_TYPES),
        ),
    )
    busy_video = _busy_shots(
        ~Shot.status.in_(video_service.VIDEO_PENDING_KEEP),
        Generation.job_type.in_(job_execution.VIDEO_JOB_TYPES),
    )
    for shot_id in busy_frame:
        with session_scope() as db:
            if generation_service.mark_frame_shot_pending(db, shot_id):
                summary["shots_recomputed"] += 1
    for shot_id in busy_video:
        with session_scope() as db:
            if video_service.mark_video_shot_pending(
                db, shot_id, keep=video_service.VIDEO_PENDING_KEEP
            ):
                summary["shots_recomputed"] += 1


def _style_extractions(now: datetime, summary: dict[str, int]) -> None:
    """(h) style extractions without a ledger (global kits, R2-M2)."""
    from app.models.style import StyleExtraction

    no_ledger = ~exists().where(Generation.id == StyleExtraction.id)
    stuck = and_(
        StyleExtraction.status == "queued",
        StyleExtraction.created_at
        < now - timedelta(seconds=defaults.RECONCILE_P1_STUCK_AFTER_SEC),
    )
    stranded = and_(
        StyleExtraction.status == "running",
        StyleExtraction.created_at < now - timedelta(seconds=defaults.LEASE_TTL_SEC),
    )
    stale = and_(
        StyleExtraction.status == "queued",
        StyleExtraction.created_at
        < now - timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC),
    )
    with session_scope() as db:
        doomed = (
            db.execute(
                select(StyleExtraction.id).where(no_ledger, or_(stuck, stranded))
            )
            .scalars()
            .all()
        )
    for job_id in doomed:
        with session_scope() as db:
            result = db.execute(
                update(StyleExtraction)
                .where(StyleExtraction.id == job_id, no_ledger, or_(stuck, stranded))
                .values(
                    status="failed",
                    error_code=ErrorCode.PROVIDER_ERROR,
                    error_message=_STUCK_MESSAGE,
                    finished_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
        summary["style_failed"] += 1
        try:
            get_bridge().publish(
                CHANNEL,
                "job.failed",
                {
                    "job_id": str(job_id),
                    "job_type": "style_extract",
                    "status": "failed",
                    "error_code": ErrorCode.PROVIDER_ERROR,
                    "error_message": _STUCK_MESSAGE,
                    "retryable": False,
                    "retry_count": 0,
                    "max_retries": 0,
                },
            )
        except Exception:  # noqa: BLE001 - SSE best effort; the row is committed
            log.warning("job.failed publish skipped job_id=%s", job_id)

    with session_scope() as db:
        ids = (
            db.execute(select(StyleExtraction.id).where(no_ledger, stale))
            .scalars()
            .all()
        )
    for job_id in ids:
        with session_scope() as db:
            # Re-check in this transaction; the send happens after its commit.
            still = db.execute(
                select(StyleExtraction.id).where(StyleExtraction.id == job_id, stale)
            ).first()
            if still is None:
                continue
            job_execution.schedule_dispatch(db, "q.p1.run_style_extract", str(job_id))
        summary["style_redispatched"] += 1


def _stuck_p1(now: datetime, summary: dict[str, int]) -> None:
    cutoff = now - timedelta(seconds=defaults.RECONCILE_P1_STUCK_AFTER_SEC)
    with session_scope() as db:
        ids = (
            db.execute(
                select(Generation.id).where(
                    Generation.status == "queued",
                    Generation.job_type.in_(job_execution.P1_LEDGER_JOB_TYPES),
                    Generation.queued_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
    for row_id in ids:
        where = [Generation.status == "queued", Generation.queued_at < cutoff]
        with session_scope() as db:
            row = _fail_row(
                db, Generation, row_id, where, ErrorCode.PROVIDER_ERROR, _STUCK_MESSAGE
            )
        if row is not None:
            summary["stuck"] += 1
            _publish_failed(Generation, row, ErrorCode.PROVIDER_ERROR, _STUCK_MESSAGE)


def _stale_queued(model: type, now: datetime, summary: dict[str, int]) -> None:
    cutoff = now - timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC)
    born = Generation.queued_at if model is Generation else AudioGeneration.created_at
    last_sent = func.coalesce(model.dispatched_at, born)
    query = select(model.id).where(model.status == "queued", last_sent < cutoff)
    if model is Generation:
        query = query.where(Generation.is_batch.is_(False))
    with session_scope() as db:
        ids = db.execute(query).scalars().all()
    for row_id in ids:
        with session_scope() as db:
            result = db.execute(
                update(model)
                .where(model.id == row_id, model.status == "queued", last_sent < cutoff)
                .values(dispatched_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
            target = _target(model, db.get(model, row_id))
            if target is None:
                continue
            job_execution.schedule_dispatch(db, *target)
        summary["redispatched"] += 1


def _renders(now: datetime, summary: dict[str, int]) -> None:
    query = select(Timeline.id, Timeline.exec_token, Timeline.heartbeat_at).where(
        Timeline.status == "rendering",
        or_(
            Timeline.exec_token.is_(None),
            and_(
                Timeline.lease_expires_at.is_not(None),
                Timeline.lease_expires_at < now,
            ),
        ),
    )
    with session_scope() as db:
        candidates = db.execute(query).all()

    for tid, token, started in candidates:
        if token is not None and started is None:
            # Requested, never started (broker lost it or it is still
            # waiting): re-send and push the deadline one lease further.
            with session_scope() as db:
                result = db.execute(
                    update(Timeline)
                    .where(
                        Timeline.id == tid,
                        Timeline.status == "rendering",
                        Timeline.exec_token == token,
                        Timeline.heartbeat_at.is_(None),
                        Timeline.lease_expires_at < now,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(seconds=defaults.LEASE_TTL_SEC)
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    continue
                job_execution.schedule_dispatch(db, "q.post.render_timeline", str(tid))
            summary["renders_redispatched"] += 1
            continue

        if token is None:
            owner = [Timeline.exec_token.is_(None)]
            message = _RENDER_LEGACY_MESSAGE
        else:
            owner = [
                Timeline.exec_token == token,
                Timeline.heartbeat_at.is_not(None),
                Timeline.lease_expires_at < now,
            ]
            message = _RENDER_LOST_MESSAGE
        with session_scope() as db:
            result = db.execute(
                update(Timeline)
                .where(Timeline.id == tid, Timeline.status == "rendering", *owner)
                .values(
                    status="failed",
                    error_code=ErrorCode.PROVIDER_ERROR,
                    error_message=message,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
        summary["renders_failed"] += 1
        try:
            get_bridge().publish(
                CHANNEL,
                "render.completed",
                {"timeline_id": str(tid), "output_path": "", "status": "failed"},
            )
        except Exception:  # noqa: BLE001 - SSE best effort; the row is committed
            log.warning("render.completed publish skipped timeline_id=%s", tid)


def _multishots(now: datetime, summary: dict[str, int]) -> None:
    from app.services import multishot as multishot_service

    cutoff = now - timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC)
    last_sent = func.coalesce(MultishotSet.dispatched_at, MultishotSet.created_at)
    with session_scope() as db:
        queued = (
            db.execute(
                select(MultishotSet.id).where(
                    MultishotSet.status == "queued", last_sent < cutoff
                )
            )
            .scalars()
            .all()
        )
        running = (
            db.execute(select(MultishotSet.id).where(MultishotSet.status == "running"))
            .scalars()
            .all()
        )
    for set_id in queued:
        with session_scope() as db:
            result = db.execute(
                update(MultishotSet)
                .where(
                    MultishotSet.id == set_id,
                    MultishotSet.status == "queued",
                    last_sent < cutoff,
                )
                .values(dispatched_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
            job_execution.schedule_dispatch(db, "q.p1.run_multishot", str(set_id))
        summary["multishot_redispatched"] += 1
    for set_id in running:
        with session_scope() as db:
            if multishot_service.reaggregate(db, set_id):
                summary["multishot_aggregated"] += 1


def reconcile_once(now: datetime | None = None) -> dict[str, int]:
    """One reconcile pass. Returns counts per action (for logs/tests)."""
    now = now or _now()
    summary = {
        "uncertain": 0,
        "requeued": 0,
        "failed": 0,
        "stuck": 0,
        "redispatched": 0,
        "renders_failed": 0,
        "renders_redispatched": 0,
        "multishot_redispatched": 0,
        "multishot_aggregated": 0,
        "batch_parents_aggregated": 0,
        "shots_recomputed": 0,
        "style_redispatched": 0,
        "style_failed": 0,
    }
    for model in (Generation, AudioGeneration):
        _expired_running(model, now, summary)
        _legacy_running(model, now, summary)
    _stuck_p1(now, summary)
    for model in (Generation, AudioGeneration):
        _stale_queued(model, now, summary)
    _style_extractions(now, summary)
    _renders(now, summary)
    _multishots(now, summary)
    _aggregates(summary)
    if any(summary.values()):
        log.info("reconcile %s", summary)
    return summary
