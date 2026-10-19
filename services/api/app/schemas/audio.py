"""Pydantic schemas for audio upload registration + placement (design §4.14)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AudioRegisterIn(BaseModel):
    asset_id: uuid.UUID
    kind: Literal["bgm", "sfx"] = "bgm"


class AudioOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    output_path: str | None
    duration_sec: float | None
    status: str


class AudioClipCreate(BaseModel):
    audio_id: uuid.UUID
    start_sec: float = Field(ge=0)
    duration_sec: float | None = Field(default=None, gt=0)
    gain_db: float = Field(default=0.0, ge=-40.0, le=12.0)


class AudioClipOut(BaseModel):
    id: uuid.UUID
    timeline_id: uuid.UUID
    audio_id: uuid.UUID
    kind: str
    start_sec: float
    duration_sec: float | None
    gain_db: float
