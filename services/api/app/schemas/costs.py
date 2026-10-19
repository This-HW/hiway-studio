"""Pydantic schemas for cost estimation + budget guard (F-15, design §9)."""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class CostEstimateIn(BaseModel):
    project_id: uuid.UUID
    items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Planned items: [{kind: 'image'|'video'|'upscale'|'outpaint'|'audio'|'render', model_id?, duration_sec?, resolution?}]",
    )


class CostEstimateItem(BaseModel):
    kind: str
    model_id: str
    quantity: int = 1
    estimated_cost_usd: float


class CostEstimateOut(BaseModel):
    project_id: uuid.UUID
    items: list[CostEstimateItem]
    total_estimated_usd: float
    budget_usd: float | None
    budget_usage_after_pct: float | None


class CostSummaryOut(BaseModel):
    project_id: uuid.UUID
    period_start: str
    period_end: str
    by_kind: dict[str, float]
    total_actual_usd: float
    total_estimated_usd: float
    budget_usd: float | None
    budget_usage_pct: float | None


class BudgetSetIn(BaseModel):
    monthly_budget_usd: float | None = Field(default=None, ge=0)
