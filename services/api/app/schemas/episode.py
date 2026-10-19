"""Episode schemas (design report §4.3, §8.1, P1-10)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EpisodeCreate(BaseModel):
    order_index: int | None = None  # None = append at end
    title: str | None = None
    is_template: bool = False


class EpisodeUpdate(BaseModel):
    order_index: int | None = None
    title: str | None = None
    is_template: bool | None = None
    # Contract enum episode_status: draft | in_progress | done.
    status: str | None = None


class EpisodeRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    order_index: int
    title: str | None
    is_template: bool
    template_source_episode_id: uuid.UUID | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EpisodeCloneResponse(BaseModel):
    episode_id: uuid.UUID


class SubtitleEditApplyRequest(BaseModel):
    """Analyse subtitle cues against a timeline's cut list (P1-6, design §8.5 P1-6).

    Analysis + proposal only: the API never rewrites the timeline. The user
    confirms by PATCHing /timelines/{id} with adjusted clip durations.
    """

    timeline_id: uuid.UUID
    # Characters per second used to estimate speech duration from cue text
    # length ([가정] default; the UI can tune it per language).
    cps: float = Field(default=12.0, gt=0)


class SubtitleClipAdjustment(BaseModel):
    cue_id: uuid.UUID
    shot_id: uuid.UUID
    cue_start_sec: float
    cue_end_sec: float
    cue_text_len: int
    current_dur_sec: float
    suggested_dur_sec: float
    fits: bool


class SubtitleEditApplyResponse(BaseModel):
    track_id: uuid.UUID
    timeline_id: uuid.UUID
    adjustments: list[SubtitleClipAdjustment]
    warnings: list[str]
