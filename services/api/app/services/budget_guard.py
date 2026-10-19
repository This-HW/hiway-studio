"""Budget guard + cost summary (F-15, design §9).

- `check_budget`: called before any new generation job is enqueued. When the
  project's `monthly_budget_usd` is set and spent + the new estimate exceeds
  it, raises 403 BUDGET_EXCEEDED. Crossing 80% usage publishes `cost.alert`.
- `spent_usd` / `summary`: aggregate actuals from generations +
  audio_generations + llm_calls (design §9.4).
- `set_budget`: PUT /projects/{id}/budget.

Period: the current UTC calendar month (documented; single-user P0).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge


def _month_bounds_utc() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = calendar.monthrange(now.year, now.month)[1]
    end = start.replace(day=last, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def _spent_by_kind(db: Session, project_id: uuid.UUID) -> dict[str, Decimal]:
    from app.models.audio import AudioGeneration
    from app.models.generation import Generation
    from app.models.system import LLMCall

    _JOB_KIND = {
        "text_to_image": "image", "image_to_video": "video",
        "text_to_video": "video", "inpaint": "inpaint",
        "upscale": "upscale", "outpaint": "outpaint",
    }

    by_kind: dict[str, Decimal] = {}
    start, end = _month_bounds_utc()
    gens = (
        db.query(Generation)
        .filter(Generation.project_id == project_id, Generation.status == "succeeded",
                Generation.created_at >= start, Generation.created_at <= end)
        .all()
    )
    for g in gens:
        amount = g.actual_cost_usd if g.actual_cost_usd is not None else (g.estimated_cost_usd or Decimal(0))
        kind = _JOB_KIND.get(g.job_type or "", "other")
        by_kind[kind] = by_kind.get(kind, Decimal(0)) + Decimal(str(amount))
    audios = (
        db.query(AudioGeneration)
        .filter(AudioGeneration.project_id == project_id, AudioGeneration.status == "done",
                AudioGeneration.created_at >= start, AudioGeneration.created_at <= end)
        .all()
    )
    for a in audios:
        amount = a.actual_cost_usd if a.actual_cost_usd is not None else (a.estimated_cost_usd or Decimal(0))
        by_kind["audio"] = by_kind.get("audio", Decimal(0)) + Decimal(str(amount))
    calls = (
        db.query(LLMCall)
        .filter(LLMCall.project_id == project_id,
                LLMCall.created_at >= start, LLMCall.created_at <= end)
        .all()
    )
    for c in calls:
        by_kind["llm"] = by_kind.get("llm", Decimal(0)) + Decimal(str(c.estimated_cost_usd or 0))
    return by_kind


def spent_usd(db: Session, project_id: uuid.UUID) -> Decimal:
    return sum(_spent_by_kind(db, project_id).values(), Decimal(0))


def get_project(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def check_budget(
    db: Session, project_id: uuid.UUID, *, estimated_new: Decimal | float = 0
) -> None:
    """Block new jobs at 100% budget; alert once when crossing 80%."""
    project = get_project(db, project_id)
    if project.monthly_budget_usd is None:
        return
    budget = Decimal(str(project.monthly_budget_usd))
    if budget <= 0:
        return
    spent = spent_usd(db, project_id)
    new = Decimal(str(estimated_new or 0))
    before_pct = spent / budget
    after_pct = (spent + new) / budget
    if after_pct >= 1:
        raise StudioError(
            ErrorCode.BUDGET_EXCEEDED,
            "Monthly budget reached: this job would push spend to 100% or more.",
            details={
                "spent_usd": float(spent),
                "budget_usd": float(budget),
                "estimated_new_usd": float(new),
            },
        )
    if before_pct < Decimal("0.8") <= after_pct:
        get_bridge().publish(
            CHANNEL, "cost.alert",
            {"project_id": str(project_id), "usage_pct": round(float(after_pct) * 100, 1),
             "spent_usd": float(spent + new), "budget_usd": float(budget),
             "threshold_pct": 80},
        )


def set_budget(db: Session, project_id: uuid.UUID, monthly_budget_usd: float | None) -> Any:
    project = get_project(db, project_id)
    project.monthly_budget_usd = (
        None if monthly_budget_usd is None else Decimal(str(monthly_budget_usd))
    )
    db.flush()
    return project


def summary(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    from app.models.generation import Generation

    get_project(db, project_id)
    by_kind_dec = _spent_by_kind(db, project_id)
    total_actual = sum(by_kind_dec.values(), Decimal(0))
    total_estimated = (
        db.query(func.coalesce(func.sum(Generation.estimated_cost_usd), 0))
        .filter(Generation.project_id == project_id)
        .scalar()
        or Decimal(0)
    )
    project = get_project(db, project_id)
    budget = Decimal(str(project.monthly_budget_usd)) if project.monthly_budget_usd else None
    usage_pct = (float(total_actual / budget * 100) if budget and budget > 0 else None)
    start, end = _month_bounds_utc()
    return {
        "project_id": project_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "by_kind": {k: float(v) for k, v in sorted(by_kind_dec.items())},
        "total_actual_usd": float(total_actual),
        "total_estimated_usd": float(Decimal(str(total_estimated))),
        "budget_usd": float(budget) if budget is not None else None,
        "budget_usage_pct": round(usage_pct, 1) if usage_pct is not None else None,
    }
