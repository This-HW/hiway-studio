"""World bible schemas (Phase 6, P2)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class BibleEntryCreate(BaseModel):
    category: str = Field(description="character|faction|location|rule|event|item")
    title: str = Field(min_length=1)
    body: str = ""
    tags: list[str] = []


class BibleEntryUpdate(BaseModel):
    category: str | None = None
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None


class BibleEntryRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    category: str
    title: str
    body: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
