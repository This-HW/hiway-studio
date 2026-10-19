"""Thumbnail generator schemas (P1-9, design §8.1, §10.5)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class ThumbnailRequest(BaseModel):
    timeline_id: str | None = Field(
        default=None, description="Optional timeline to base thumbnails on."
    )
    title_text: str | None = Field(
        default=None,
        max_length=60,
        description="Overlay text. Rendered by post-processing (PIL), never by the image model (§10.5).",
    )
    count: int = Field(default=4, ge=2, le=8)


class ThumbnailAcceptedResponse(BaseModel):
    """202 response: job_id is the batch parent generation id."""

    job_id: uuid.UUID
    estimated_cost_usd: Decimal
    count: int
