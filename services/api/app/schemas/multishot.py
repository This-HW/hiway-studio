"""Multishot coverage schemas (P1-1, design §8.1, F-09)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class MultishotRequest(BaseModel):
    model_id: str | None = Field(
        default=None, description="Explicit image model; defaults to project routing."
    )


class MultishotAcceptedResponse(BaseModel):
    multishot_set_id: uuid.UUID
    job_id: uuid.UUID
    estimated_cost_usd: Decimal


class MultishotFrameRead(BaseModel):
    id: uuid.UUID
    coverage_type: str
    status: str
    approved: bool
    generation_id: uuid.UUID | None
    shot_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class MultishotSetRead(BaseModel):
    id: uuid.UUID
    shot_id: uuid.UUID
    status: str
    frames: list[MultishotFrameRead] = Field(default_factory=list)

    model_config = {"from_attributes": True}
