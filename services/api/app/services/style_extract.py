"""Style extraction (P1-7, design §8.1).

Flow:
  1. POST /style-kits/{id}/extract -> request_extract(): validates the source
     assets, applies the 3-8 rule (<3: 202 + warning and the job still runs;
     >8: only the first 8 are used), checks the budget (per_call), and creates
     ONE job row per request (`style_extractions`, plan D9 / ADR-0013): the
     input snapshot (source ids) lives on that row. A project kit also gets a
     generations cost-ledger row (job_type='style_extract') with the SAME id,
     so /jobs/{id} retry/cancel address the same logical job. The task
     argument is that job id (ADR-0008) — never the kit id.
  2. complete_extract(job_id) (worker): locks the ledger (project kits),
     claims exactly that row (queued -> running, conditional UPDATE), checks
     the ledger is still queued, writes the kit fields (stub values) + the
     row's source snapshot, bumps the kit version and finishes the row and
     ledger — all in one transaction. A terminal row (done, failed, canceled)
     is a no-op, so a redelivered message never completes a different (newer)
     extraction and a global kit's message applies once.

Lock order (R2-M1, every path — worker, crash failure, retry, cancel,
reconcile): ledger generation row -> style_extractions row -> style kit row.
cancel / retry / reconcile reach the job row only through the ledger
(job_execution.on_generation_terminal, generations._retry_dependents), so the
worker and fail_extract lock the ledger first too (no 40P01 deadlock).

Locked kits reject extraction (STYLE_KIT_LOCKED), mirroring update_style_kit.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import dispatch_after_commit
from app.models.style import StyleKit
from app.services import budget_guard, cost_estimator

# Per-call priced extraction model (seed; DB overlay can replace the pricing).
EXTRACT_MODEL_ID = "stub.style-extract"

JOB_TYPE = "style_extract"

MIN_SOURCES = 3
MAX_SOURCES = 8

# Stub extraction result: fixed sample values, clearly synthetic. A real
# implementation derives these from the source images via a vision model.
STUB_EXTRACTION: dict[str, str] = {
    "color": "warm amber highlights with deep teal shadows; muted pastel palette",
    "lighting": "soft volumetric key light from the left, gentle rim light",
    "texture": "fine film grain, painterly brush strokes in backgrounds",
    "composition": "rule-of-thirds subject placement, generous headroom",
    "lens": "35mm, shallow depth of field, subtle anamorphic flare",
    "motion": "slow push-in, handheld micro-jitter",
    "editing_rhythm": "lingering 4-6s holds, match cuts on motion",
    "sound": "warm analog synth pads, soft foley",
}


def _get_kit(db: Session, kit_id: uuid.UUID) -> StyleKit:
    kit = db.get(StyleKit, kit_id)
    if kit is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Style kit not found.")
    return kit


def _publish(job_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    get_bridge().publish(CHANNEL, event, {"job_id": str(job_id), "job_type": JOB_TYPE, **data})


def request_extract(
    db: Session, kit_id: uuid.UUID, *, source_asset_ids: list[uuid.UUID]
) -> dict[str, Any]:
    from app.models.asset import Asset
    from app.models.generation import Generation

    kit = _get_kit(db, kit_id)
    if kit.is_locked:
        raise StudioError(
            ErrorCode.STYLE_KIT_LOCKED,
            "Style kit is locked. Duplicate it to edit.",
        )
    assets = []
    for aid in source_asset_ids:
        asset = db.get(Asset, aid)
        if asset is None:
            raise StudioError(
                ErrorCode.NOT_FOUND,
                "Source asset not found.",
                details={"source_asset_id": str(aid)},
            )
        assets.append(asset)

    warning: str | None = None
    used = assets
    if len(assets) < MIN_SOURCES:
        warning = (
            f"Only {len(assets)} source image(s) given; {MIN_SOURCES}-{MAX_SOURCES} "
            "recommended. Proceeding anyway."
        )
    elif len(assets) > MAX_SOURCES:
        used = assets[:MAX_SOURCES]

    check_enum("job_type", JOB_TYPE)
    estimated = cost_estimator.estimate(db, EXTRACT_MODEL_ID)
    if kit.project_id is not None:
        budget_guard.check_budget(db, kit.project_id, estimated_new=estimated)

    from app.models.style import StyleExtraction

    job_id = uuid.uuid4()
    db.add(StyleExtraction(
        id=job_id, style_kit_id=kit.id, status="queued",
        source_ids=[str(a.id) for a in used],
    ))
    ledger_id: uuid.UUID | None = None
    if kit.project_id is not None:
        gen = Generation(
            id=job_id,  # same logical job id (retry/cancel via /jobs/{id})
            project_id=kit.project_id,
            shot_id=None,
            job_type=JOB_TYPE,
            model_id=EXTRACT_MODEL_ID,
            estimated_cost_usd=estimated,
            params={"style_kit_id": str(kit.id), "style_extraction_id": str(job_id)},
        )
        db.add(gen)
        ledger_id = gen.id
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.p1.run_style_extract", str(job_id))
    )
    return {
        "job_id": job_id,
        "style_kit_id": kit.id,
        "estimated_cost_usd": estimated,
        "warning": warning,
        "images_used": len(used),
        "ledger_generation_id": ledger_id,
    }


def _ledger(db: Session, job_id: uuid.UUID):
    from app.models.generation import Generation

    gen = db.get(Generation, job_id)
    return gen if gen is not None and gen.job_type == JOB_TYPE else None


def complete_extract(db: Session, job_id: str) -> dict[str, Any]:
    """Worker: run THIS extraction once and write the kit fields (stub values).

    The ledger (project kits) is row-locked FIRST (lock order above), then
    the claim = conditional UPDATE queued -> running on the job row
    (Postgres row-locks it; a concurrent duplicate re-evaluates after commit
    and gets 0 rows; SQLite serializes writers). The ledger must still be
    queued, else the job ends with the ledger's terminal state. The kit
    fields, version bump, job and ledger completion commit together with the
    claim (caller's transaction), so a crash rolls everything back.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.models.generation import Generation
    from app.models.style import StyleExtraction
    from app.services.generations import lock_row

    jid = uuid.UUID(str(job_id))
    ledger = _ledger(db, jid)
    if ledger is not None:
        ledger = lock_row(db, Generation, jid)  # 1st lock: ledger (R2-M1)
    claimed = db.execute(
        update(StyleExtraction)
        .where(StyleExtraction.id == jid, StyleExtraction.status == "queued")
        .values(status="running")
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    job = db.get(StyleExtraction, jid, populate_existing=True)
    if job is None:
        return {"job_id": str(jid), "status": "missing", "noop": True}
    if not claimed:
        return {"job_id": str(jid), "style_kit_id": str(job.style_kit_id),
                "status": job.status, "noop": True}

    now = datetime.now(timezone.utc)
    if ledger is not None:
        if ledger.status != "queued":
            # Canceled / failed (reconcile) meanwhile: never apply the effect.
            job.status = "canceled" if ledger.status == "canceled" else "failed"
            job.error_code = ledger.error_code or "canceled"
            job.error_message = ledger.error_message
            job.finished_at = now
            db.flush()
            return {"job_id": str(jid), "style_kit_id": str(job.style_kit_id),
                    "status": job.status, "noop": True}

    kit = db.get(StyleKit, job.style_kit_id, with_for_update=True, populate_existing=True)
    if kit is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Style kit not found.")
    _publish(jid, "job.progress",
             {"status": "running", "progress_pct": 50.0, "stage": "extracting"})

    # The stub writes fixed sample values; a real backend analyses the source
    # images of THIS job's snapshot (job.source_ids) with a vision model here.
    for field, value in STUB_EXTRACTION.items():
        setattr(kit, field, value)
    kit.extraction_source_ids = list(job.source_ids or [])
    kit.version += 1
    job.status = "succeeded"
    job.finished_at = now
    if ledger is not None:
        ledger.status = "succeeded"
        ledger.progress_pct = 100.0
        ledger.actual_cost_usd = ledger.estimated_cost_usd
        ledger.finished_at = now
    db.flush()

    _publish(jid, "job.completed",
             {"status": "succeeded", "output_count": 1, "style_kit_id": str(kit.id),
              "extracted_fields": sorted(STUB_EXTRACTION)})
    return {"job_id": str(jid), "style_kit_id": str(kit.id), "status": "succeeded",
            "extracted_fields": sorted(STUB_EXTRACTION)}


def fail_extract(db: Session, job_id: str, code: str, message: str) -> bool:
    """Commit a crashed extraction as failed (job row + ledger, if queued)."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.models.generation import Generation
    from app.models.style import StyleExtraction

    jid = uuid.UUID(str(job_id))
    now = datetime.now(timezone.utc)
    # Lock order (R2-M1): ledger first, then the job row.
    db.execute(
        update(Generation)
        .where(Generation.id == jid, Generation.job_type == JOB_TYPE,
               Generation.status.in_(("queued", "running")))
        .values(status="failed", error_code=code, error_message=message[:500],
                finished_at=now, exec_token=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    hit = db.execute(
        update(StyleExtraction)
        .where(StyleExtraction.id == jid,
               StyleExtraction.status.in_(("queued", "running")))
        .values(status="failed", error_code=code, error_message=message[:500],
                finished_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    return hit


def on_ledger_terminal(db: Session, gen: Any) -> None:
    """The ledger ended without the worker (cancel / reconcile): end its job row."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.models.style import StyleExtraction

    db.execute(
        update(StyleExtraction)
        .where(StyleExtraction.id == gen.id,
               StyleExtraction.status.in_(("queued", "running")))
        .values(status="canceled" if gen.status == "canceled" else "failed",
                error_code=gen.error_code, error_message=gen.error_message,
                finished_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )


def require_job_row(db: Session, gen: Any) -> None:
    """Reject a retry of a ledger that has no style_extractions row (R2-M3).

    Ledgers from before migration 0014 carry no input snapshot; re-queueing
    them would dispatch a message the worker ignores (silent no-op). The
    snapshot is not reconstructed — the user re-requests the extraction.
    """
    from app.models.style import StyleExtraction

    if db.get(StyleExtraction, gen.id) is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "This style extraction predates job tracking and cannot be retried; "
            "re-request extraction from the style kit.",
            details={"job_id": str(gen.id), "reason": "no_extraction_job_row"},
        )


def on_ledger_retried(db: Session, gen: Any) -> None:
    """A re-queued ledger (manual retry): its job row is queued again."""
    from sqlalchemy import update

    from app.models.style import StyleExtraction

    db.execute(
        update(StyleExtraction)
        .where(StyleExtraction.id == gen.id,
               StyleExtraction.status.in_(("failed", "canceled")))
        .values(status="queued", error_code=None, error_message=None, finished_at=None)
        .execution_options(synchronize_session=False)
    )
