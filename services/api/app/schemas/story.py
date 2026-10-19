"""Story / beat / episode schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class StoryRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    logline: str | None
    synopsis: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StoryUpdate(BaseModel):
    logline: str | None = None
    synopsis: str | None = None


class SynopsisGenerateRequest(BaseModel):
    model_id: str | None = None
    logline: str = Field(min_length=1)


class SynopsisGenerateResponse(BaseModel):
    llm_call_id: uuid.UUID
    synopsis: str


class BeatsGenerateRequest(BaseModel):
    model_id: str | None = None
    act_count: int | None = Field(default=None, ge=1, le=5)


class BeatCreate(BaseModel):
    order_index: int | None = None  # None = append at end
    act: int | None = None
    title: str | None = None
    summary: str | None = None


class BeatUpdate(BaseModel):
    order_index: int | None = None
    act: int | None = None
    title: str | None = None
    summary: str | None = None


class BeatRead(BaseModel):
    id: uuid.UUID
    story_id: uuid.UUID
    order_index: int
    act: int | None
    title: str | None
    summary: str | None

    model_config = {"from_attributes": True}


class BeatsGenerateResponse(BaseModel):
    llm_call_id: uuid.UUID
    beats: list[BeatRead]


class ScriptImportResponse(BaseModel):
    scenes_created: int
