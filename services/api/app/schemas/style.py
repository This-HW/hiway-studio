"""Style kit and category pack schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

STYLE_KIT_FIELDS = (
    "color",
    "lighting",
    "texture",
    "composition",
    "lens",
    "motion",
    "editing_rhythm",
    "sound",
)


class StyleKitCreate(BaseModel):
    name: str = Field(min_length=1)
    color: str | None = None
    lighting: str | None = None
    texture: str | None = None
    composition: str | None = None
    lens: str | None = None
    motion: str | None = None
    editing_rhythm: str | None = None
    sound: str | None = None


class StyleKitUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    lighting: str | None = None
    texture: str | None = None
    composition: str | None = None
    lens: str | None = None
    motion: str | None = None
    editing_rhythm: str | None = None
    sound: str | None = None


class StyleKitRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    version: int
    is_locked: bool
    color: str | None
    lighting: str | None
    texture: str | None
    composition: str | None
    lens: str | None
    motion: str | None
    editing_rhythm: str | None
    sound: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CategoryPackRead(BaseModel):
    code: str
    name: str
    description: str | None
    pipeline_config: dict
    is_builtin: bool

    model_config = {"from_attributes": True}
