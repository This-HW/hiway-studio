"""Video / timeline / export-preset schemas (F-10, F-12, F-15, P0-13/14/18)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class VideoGenerateRequest(BaseModel):
    # i2v (default) | t2v. i2v needs an approved frame (409 otherwise).
    mode: str = Field(default="i2v")
    # Motion rule (§5.9): describe ONLY what must move/change.
    motion_prompt: str = Field(min_length=1, max_length=2000)
    duration_sec: float | None = Field(default=None, gt=0)
    model_id: str | None = None


class VideoRead(BaseModel):
    id: uuid.UUID
    shot_id: uuid.UUID
    generation_id: uuid.UUID | None
    mode: str
    motion_prompt: str | None
    duration_sec: float
    fps: int
    resolution: str
    model_id: str
    output_path: str
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class VideoAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    video_id: uuid.UUID
    estimated_cost_usd: Decimal


class ClipEntry(BaseModel):
    shot_id: uuid.UUID
    video_id: uuid.UUID
    in_sec: float = Field(ge=0)
    out_sec: float = Field(gt=0)


class TimelineCreate(BaseModel):
    name: str | None = None


class TimelineUpdate(BaseModel):
    name: str | None = None
    clip_order: list[ClipEntry] | None = None
    subtitle_mode: str | None = None
    subtitle_track_ids: list[str] | None = None


class TimelineRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    # Assembled from timeline_clips in position order (ADR-0007); the DB
    # model has no clip_order column. Routers fill this via get_clip_order.
    clip_order: list[dict[str, Any]] = []
    subtitle_mode: str
    subtitle_track_ids: list[str] | None
    output_preset_id: uuid.UUID | None
    output_path: str | None
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RenderRequest(BaseModel):
    export_preset_id: uuid.UUID
    # QC gate (ADR-0006): a 'warn' report requires this flag to render.
    ignore_warnings: bool = False


class RenderAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    estimated_cost_usd: Decimal


class ExportPresetCreate(BaseModel):
    name: str = Field(min_length=1)
    aspect_ratio: str
    resolution: str = Field(min_length=1)
    fps: int = Field(default=24, gt=0)
    codec: str = "h264"
    audio_codec: str = "aac"
    burn_subtitles: bool = False


class ExportPresetUpdate(BaseModel):
    name: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    fps: int | None = Field(default=None, gt=0)
    codec: str | None = None
    audio_codec: str | None = None
    burn_subtitles: bool | None = None


class ExportPresetRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    platform: str | None
    aspect_ratio: str
    resolution: str
    fps: int
    codec: str
    audio_codec: str
    burn_subtitles: bool
    builtin: bool = False

    model_config = {"from_attributes": True}
