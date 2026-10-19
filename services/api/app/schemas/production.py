"""Scene / shot / shot-reference schemas (P0-9, design §4.4–§4.6, §8.1)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SceneCreate(BaseModel):
    title: str | None = None
    location: str | None = None
    time_of_day: str | None = None
    mood: str | None = None
    lighting: str | None = None
    beat_id: uuid.UUID | None = None
    style_kit_id: uuid.UUID | None = None


class SceneUpdate(BaseModel):
    title: str | None = None
    location: str | None = None
    time_of_day: str | None = None
    mood: str | None = None
    lighting: str | None = None
    style_kit_id: uuid.UUID | None = None


class ShotCreate(BaseModel):
    shot_type: str | None = None
    camera_angle: str | None = None
    movement: str | None = None
    duration_sec: float = Field(default=5.0, gt=0)
    action: str | None = None
    dialogue: str | None = None
    emotion_weights: dict[str, float] | None = None
    sfx_notes: str | None = None
    intended_text: str | None = None
    style_kit_id: uuid.UUID | None = None
    # P1-3: pinned seed for reproducible generation (NULL = random per call).
    seed: int | None = None
    # P1-2: camera preset + 180-degree rule side (left|right|center).
    camera_preset_id: uuid.UUID | None = None
    camera_side: str | None = None


class ShotUpdate(BaseModel):
    shot_type: str | None = None
    camera_angle: str | None = None
    movement: str | None = None
    duration_sec: float | None = Field(default=None, gt=0)
    action: str | None = None
    dialogue: str | None = None
    emotion_weights: dict[str, float] | None = None
    sfx_notes: str | None = None
    intended_text: str | None = None
    style_kit_id: uuid.UUID | None = None
    order_index: int | None = None
    seed: int | None = None
    camera_preset_id: uuid.UUID | None = None
    camera_side: str | None = None


class ShotRead(BaseModel):
    id: uuid.UUID
    scene_id: uuid.UUID
    order_index: int
    shot_type: str | None
    camera_angle: str | None
    movement: str | None
    duration_sec: float
    action: str | None
    dialogue: str | None
    emotion_weights: dict | None
    sfx_notes: str | None
    intended_text: str | None
    status: str
    frame_stale: bool
    approved_frame_generation_id: uuid.UUID | None
    approved_at: datetime | None
    style_kit_id: uuid.UUID | None
    seed: int | None
    camera_preset_id: uuid.UUID | None
    camera_side: str | None
    # Board card thumbnail: approved frame first, else latest take (relative path).
    thumbnail: str | None = None
    candidate_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SceneRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    order_index: int
    title: str | None
    location: str | None
    time_of_day: str | None
    mood: str | None
    lighting: str | None
    style_kit_id: uuid.UUID | None
    shots: list[ShotRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ShotReorderRequest(BaseModel):
    ordered_ids: list[uuid.UUID] = Field(min_length=1)


class ShotReferenceCreate(BaseModel):
    character_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    purpose: str
    reference_detail_id: uuid.UUID | None = None


class ShotReferenceRead(BaseModel):
    id: uuid.UUID
    shot_id: uuid.UUID
    character_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    purpose: str
    reference_detail_id: uuid.UUID | None
    # Denormalized for the UI: character/asset name and a preview image.
    target_name: str | None = None
    target_image: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
