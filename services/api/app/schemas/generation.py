"""Generation / job schemas (F-06, F-07, P0-14, design §4.7, §8.1)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class FrameGenerateRequest(BaseModel):
    model_id: str | None = None
    batch_size: int = Field(default=1, ge=1, le=4)
    # Section-level prompt overrides: keys style|category|appearance|wardrobe|
    # scene_context|shot_action|camera|continuity|negative (None clears).
    overrides: dict[str, str | None] | None = None
    use_continuity: bool = False
    seed: int | None = None


class RetakeRequest(BaseModel):
    model_id: str | None = None
    batch_size: int = Field(default=1, ge=1, le=4)
    overrides: dict[str, str | None] | None = None
    # P1-3: explicit seed pins shot.seed; None reuses the pinned seed or draws random.
    seed: int | None = None


class InpaintRequest(BaseModel):
    generation_id: uuid.UUID
    # Project-relative path of the mask image.
    mask_path: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    model_id: str | None = None


class UpscaleRequest(BaseModel):
    generation_id: uuid.UUID
    model_id: str | None = None


class OutpaintRequest(BaseModel):
    generation_id: uuid.UUID
    prompt: str = Field(min_length=1)
    mask_path: str | None = None
    direction: str | None = None
    model_id: str | None = None


class ApproveRequest(BaseModel):
    generation_id: uuid.UUID


class JobAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    estimated_cost_usd: Decimal


class ContinuityResponse(BaseModel):
    available: bool
    step: str
    reason: str
    summary: str = ""
    source_shot_id: uuid.UUID | None = None
    source_generation_id: uuid.UUID | None = None
    source_path: str | None = None


class GenerationRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    job_type: str
    status: str
    model_id: str | None
    shot_id: uuid.UUID | None
    is_batch: bool
    parent_generation_id: uuid.UUID | None
    batch_index: int | None
    progress_pct: float | None
    error_code: str | None
    error_message: str | None
    output_path: str | None
    output_thumbnail_path: str | None
    prompt_final: str | None = None
    reference_ids: list[str] | None = None
    estimated_cost_usd: Decimal | None
    actual_cost_usd: Decimal | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class BatchDetailResponse(BaseModel):
    batch: GenerationRead
    children: list[GenerationRead]
