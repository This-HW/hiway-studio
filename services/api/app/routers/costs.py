"""Cost estimation + budget summary (F-15, design §9)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import costs as s
from app.services import budget_guard, cost_estimator

router = APIRouter(tags=["costs"])

# Default model per planned-item kind when the caller omits model_id.
# These must be real seed-registry IDs (infra/seeds/models/*.json).
_KIND_DEFAULT_MODEL = {
    "image": "stub.t2i-fast",
    "video": "stub.t2v-fast",
    "upscale": "stub.t2i-fast",
    "outpaint": "stub.t2i-fast",
    "audio": "stub.audio.v1",  # unused: audio/render short-circuit to $0 below
    "render": "local.ffmpeg.v1",
}


@router.post("/costs/estimate", response_model=s.CostEstimateOut)
def estimate_plan(body: s.CostEstimateIn, db: Session = Depends(get_db)):
    items: list[s.CostEstimateItem] = []
    total = Decimal("0")
    for raw in body.items:
        kind = str(raw.get("kind", "image"))
        model_id = str(raw.get("model_id") or _KIND_DEFAULT_MODEL.get(kind, "stub.t2i-fast"))
        quantity = max(int(raw.get("quantity", 1)), 1)
        duration = float(raw.get("duration_sec", 0) or 0)
        if kind in ("image", "upscale", "outpaint"):
            cost = cost_estimator.estimate_images(db, model_id, quantity)
        elif kind == "video":
            cost = cost_estimator.estimate(db, model_id, duration_sec=duration) * quantity
        elif kind in ("audio", "render"):
            cost = Decimal("0")  # P0: local upload / local ffmpeg render are free
        else:
            cost = cost_estimator.estimate(db, model_id, duration_sec=duration) * quantity
        items.append(s.CostEstimateItem(
            kind=kind, model_id=model_id, quantity=quantity,
            estimated_cost_usd=float(cost)))
        total += cost
    project = budget_guard.get_project(db, body.project_id)
    budget = float(project.monthly_budget_usd) if project.monthly_budget_usd else None
    spent = float(budget_guard.spent_usd(db, body.project_id))
    usage_after = ((spent + float(total)) / budget * 100) if budget else None
    return s.CostEstimateOut(
        project_id=body.project_id, items=items,
        total_estimated_usd=float(total), budget_usd=budget,
        budget_usage_after_pct=round(usage_after, 1) if usage_after is not None else None,
    )


@router.get("/projects/{project_id}/costs/summary", response_model=s.CostSummaryOut)
def cost_summary(project_id: uuid.UUID, db: Session = Depends(get_db)):
    data = budget_guard.summary(db, project_id)
    return s.CostSummaryOut(**data)


@router.put("/projects/{project_id}/budget", status_code=200)
def set_budget(project_id: uuid.UUID, body: s.BudgetSetIn,
               db: Session = Depends(get_db)):
    project = budget_guard.set_budget(db, project_id, body.monthly_budget_usd)
    return {"project_id": str(project.id),
            "monthly_budget_usd": (float(project.monthly_budget_usd)
                                   if project.monthly_budget_usd is not None else None)}
