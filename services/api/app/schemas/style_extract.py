"""Style extraction schemas (P1-7, design §8.1)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class StyleExtractRequest(BaseModel):
    source_asset_ids: list[uuid.UUID] = Field(
        description="Asset ids to extract style from (3-8 recommended)."
    )


class StyleExtractAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    estimated_cost_usd: Decimal
    warning: str | None = Field(
        default=None,
        description="Set when fewer than 3 source images were given; the job still runs.",
    )
    images_used: int


class StyleExtractRead(BaseModel):
    """The kit fields after extraction (subset of StyleKitRead)."""

    id: uuid.UUID
    name: str
    version: int
    color: str | None
    lighting: str | None
    texture: str | None
    composition: str | None
    lens: str | None
    motion: str | None
    editing_rhythm: str | None
    sound: str | None
    extraction_source_ids: list | None

    model_config = {"from_attributes": True}
