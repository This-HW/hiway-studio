"""Camera preset, 180-degree check, and prompt variation schemas (P1-2, P1-3)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class CameraPresetRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    lens: str | None
    movement: str | None
    angle: str | None
    settings: dict
    is_builtin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CameraCheckViolation(BaseModel):
    shot_id: uuid.UUID
    message: str


class CameraCheckResponse(BaseModel):
    scene_id: uuid.UUID
    violations: list[CameraCheckViolation]


class VariationsRequest(BaseModel):
    count: int = Field(default=3, ge=2, le=4)
    variation_axis: Literal["prompt", "seed"] = "prompt"
    model_id: str | None = None
    # Explicit base seed: pins shot.seed; None falls back to the shot's pinned
    # seed, else a fresh random base (children always get base+i).
    seed: int | None = None


class VariationJob(BaseModel):
    job_id: uuid.UUID
    prompt_final: str
    seed: int


class VariationsResponse(BaseModel):
    job_id: uuid.UUID
    variation_job_ids: list[uuid.UUID]
    variations: list[VariationJob]
    count: int
    variation_axis: str
    model_id: str
    estimated_cost_usd: Decimal
