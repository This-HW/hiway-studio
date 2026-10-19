"""LoRA training schemas (P1-4, design §8.1)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class LoraTrainRequest(BaseModel):
    training_image_ids: list[uuid.UUID] = Field(
        description="CharacterReference ids used as training data."
    )
    rank: int | None = Field(default=None, ge=1, le=256)
    steps: int | None = Field(default=None, ge=1)
    trigger_word: str = Field(min_length=1, max_length=64)
    confirm: bool = Field(
        default=False,
        description="Required true when fewer than 8 training images are given.",
    )


class LoraTrainAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    lora_model_id: uuid.UUID
    estimated_cost_usd: Decimal


class LoraModelRead(BaseModel):
    id: uuid.UUID
    character_id: uuid.UUID
    trigger_word: str
    rank: int | None
    steps: int | None
    training_image_count: int | None
    status: str
    model_path: str | None
    base_model: str | None

    model_config = {"from_attributes": True}
