"""Project schemas (request/response live with the domain file; no separate DTOs)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AspectRatio = Literal["16:9", "9:16", "1:1", "2.39:1", "4:5"]


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1)
    aspect_ratio: AspectRatio = "16:9"
    category_pack_code: str | None = None
    default_models: dict = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    title: str | None = None
    aspect_ratio: AspectRatio | None = None
    default_models: dict | None = None
    style_kit_id: uuid.UUID | None = None
    monthly_budget_usd: Decimal | None = None


class ProjectRead(BaseModel):
    id: uuid.UUID
    title: str
    aspect_ratio: str
    default_style: str | None
    default_models: dict
    style_kit_id: uuid.UUID | None
    category_pack_code: str | None
    monthly_budget_usd: Decimal | None
    is_archived: bool
    category_pack_name: str | None = None
    episode_count: int = 0
    scene_count: int = 0
    # Project-card summary (P0_SCREENS §1), computed by services.project.card_stats.
    # Representative frame: relative path (GET /files/{path}) of the first
    # scene's first shot's approved frame, or None.
    thumbnail: str | None = None
    shot_count: int = 0
    approved_shot_count: int = 0
    clip_count: int = 0
    # Current UTC month, USD. actual == cost summary total_actual_usd.
    monthly_cost_estimated_usd: float = 0.0
    monthly_cost_actual_usd: float = 0.0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectDuplicateResponse(BaseModel):
    id: uuid.UUID
    title: str


class ProjectArchiveResponse(BaseModel):
    job_id: uuid.UUID
    status: str = "queued"
