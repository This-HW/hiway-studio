"""Pydantic schemas for subtitles (F-13, design §4.13)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SubtitleTrackCreate(BaseModel):
    language_code: str = Field(default="ko", min_length=2, max_length=8)
    label: str | None = Field(default=None, max_length=80)
    is_default: bool = False


class SubtitleTrackOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    language_code: str
    label: str | None
    is_default: bool
    created_at: datetime


class SubtitleCueIn(BaseModel):
    # NOTE: start_sec is gt=0 (not ge=0) — the DB CHECK requires start_sec > 0.
    start_sec: float = Field(gt=0)
    end_sec: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)
    style_overrides: dict[str, Any] | None = None


class SubtitleCueReplace(BaseModel):
    cues: list[SubtitleCueIn] = Field(default_factory=list, max_length=2000)


class SubtitleCueOut(BaseModel):
    id: uuid.UUID
    track_id: uuid.UUID
    start_sec: float
    end_sec: float
    text: str
    style_overrides: dict[str, Any] | None


class SubtitleStyleIn(BaseModel):
    font_family: str | None = Field(default=None, max_length=120)
    font_size_px: int | None = Field(default=None, ge=8, le=400)
    color: str | None = Field(default=None, max_length=16)
    background_color: str | None = Field(default=None, max_length=16)
    background_opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    position: Literal["top", "middle", "bottom"] | None = None
    safe_area_margin_pct: float | None = Field(default=None, ge=0.0, le=30.0)
    line_break_rule: str | None = Field(default=None, max_length=16)


class SubtitleStyleOut(BaseModel):
    """Effective style (track override wins, else project default, else built-in)."""

    font_family: str
    font_size_px: int
    color: str
    background_color: str | None
    background_opacity: float
    position: str
    safe_area_margin_pct: float
    line_break_rule: str
