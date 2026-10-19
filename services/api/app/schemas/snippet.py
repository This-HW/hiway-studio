"""Prompt snippet schemas (P1-8, design report §4.20)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PromptSnippetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    negative: str | None = Field(default=None, max_length=8000)
    category: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=20)
    # NULL = global snippet; otherwise the owning project.
    project_id: uuid.UUID | None = None


class PromptSnippetUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=8000)
    negative: str | None = Field(default=None, max_length=8000)
    category: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=20)


class PromptSnippetRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    title: str
    body: str
    negative: str | None
    category: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
