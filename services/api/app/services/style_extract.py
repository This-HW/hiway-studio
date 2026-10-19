"""Style extraction (P1-7, design §8.1).

Flow:
  1. POST /style-kits/{id}/extract -> request_extract(): validates the source
     assets, applies the 3-8 rule (<3: 202 + warning and the job still runs;
     >8: only the first 8 are used), checks the budget (per_call), records the
     source ids on the kit (extraction_source_ids), creates a generations
     ledger row (job_type='style_extract') when the kit belongs to a project,
     and dispatches q.p1.run_style_extract (task arg = the kit id, ADR-0008 —
     the row is the source of truth).
  2. complete_extract() (worker): the stub writes fixed sample values into the
     kit's style fields and bumps the version. A real backend replaces the
     stub with a vision-LLM call over the source images.

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

    kit.extraction_source_ids = [str(a.id) for a in used]
    ledger_id: uuid.UUID | None = None
    if kit.project_id is not None:
        gen = Generation(
            project_id=kit.project_id,
            shot_id=None,
            job_type=JOB_TYPE,
            model_id=EXTRACT_MODEL_ID,
            estimated_cost_usd=estimated,
            params={"style_kit_id": str(kit.id)},
        )
        db.add(gen)
        db.flush()
        ledger_id = gen.id
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.p1.run_style_extract", str(kit.id))
    )
    return {
        "job_id": kit.id,
        "estimated_cost_usd": estimated,
        "warning": warning,
        "images_used": len(used),
        "ledger_generation_id": ledger_id,
    }


def complete_extract(db: Session, kit_id: str) -> dict[str, Any]:
    """Worker: run the extraction and write the kit fields (stub values)."""
    from datetime import datetime, timezone

    from app.models.generation import Generation

    kit = _get_kit(db, uuid.UUID(kit_id))
    _publish(kit.id, "job.progress",
             {"status": "running", "progress_pct": 50.0, "stage": "extracting"})

    # The stub writes fixed sample values; a real backend analyses the source
    # images (kit.extraction_source_ids) with a vision model here.
    for field, value in STUB_EXTRACTION.items():
        setattr(kit, field, value)
    kit.version += 1
    db.flush()

    if kit.project_id is not None:
        # Oldest queued ledger for THIS kit (FIFO per kit stays correct when
        # several kits extract concurrently).
        ledgers = (
            db.query(Generation)
            .filter(
                Generation.job_type == JOB_TYPE,
                Generation.status == "queued",
                Generation.project_id == kit.project_id,
            )
            .order_by(Generation.created_at)
            .all()
        )
        gen = next(
            (g for g in ledgers if (g.params or {}).get("style_kit_id") == str(kit.id)),
            None,
        )
        if gen is not None:
            gen.status = "succeeded"
            gen.progress_pct = 100.0
            gen.actual_cost_usd = gen.estimated_cost_usd
            gen.finished_at = datetime.now(timezone.utc)
    db.flush()

    _publish(kit.id, "job.completed",
             {"status": "succeeded", "output_count": 1,
              "extracted_fields": sorted(STUB_EXTRACTION)})
    return {"style_kit_id": str(kit.id), "status": "succeeded",
            "extracted_fields": sorted(STUB_EXTRACTION)}
