"""Pydantic schemas for Phase-5 audio: TTS dialogue, voice clone, B-roll suggest.

Design report §8.1 (오디오 P1-5), §4.14, F-13.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DialogueIn(BaseModel):
    """POST /shots/{shot_id}/dialogue body (design §8.1)."""

    character_id: uuid.UUID
    text: str = Field(min_length=1, max_length=5000)
    # Emotion weights: {"joy": 0.8, "anger": 0.1, ...}; each weight in 0..1.
    # Stored on audio_generations.emotion_weights and passed through to the
    # provider params; the stub adapter ignores them (F-13).
    emotion_weights: dict[str, float] | None = Field(default=None)
    model_id: str | None = Field(default=None)
    # Optional: after TTS synthesis, fit the audio to this duration via
    # services.lipsync (±10% rate adjustment; warn beyond). NULL = no fitting.
    target_duration_sec: float | None = Field(default=None, gt=0)

    @field_validator("emotion_weights")
    @classmethod
    def _check_weights(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return v
        for name, w in v.items():
            if not name or not isinstance(name, str):
                raise ValueError("emotion_weights keys must be non-empty strings.")
            if not isinstance(w, (int, float)) or not 0.0 <= float(w) <= 1.0:
                raise ValueError(
                    f"emotion weight for '{name}' must be in 0..1, got {w!r}."
                )
        return v


class DialogueAccepted(BaseModel):
    job_id: uuid.UUID
    estimated_cost_usd: str | None = None


class VoiceCloneIn(BaseModel):
    sample_asset_id: uuid.UUID


class VoiceCloneOut(BaseModel):
    voice_id: str


class BrollSuggestIn(BaseModel):
    timeline_id: uuid.UUID
    limit: int = Field(default=5, ge=1, le=20)


class BrollSuggestion(BaseModel):
    asset_id: uuid.UUID
    asset_name: str
    keyword: str
    score: float


class BrollSuggestOut(BaseModel):
    suggestions: list[BrollSuggestion]
    keywords: list[str]
    narration_chars: int


class AudioJobRead(BaseModel):
    """Read view of an audio_generations row (TTS dialogue)."""

    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID | None
    character_id: uuid.UUID | None
    kind: str
    text: str | None
    voice_id: str | None
    emotion_weights: dict[str, Any] | None
    model_id: str | None
    output_path: str | None
    duration_sec: float | None
    status: str
    error_code: str | None
    error_message: str | None
    estimated_cost_usd: str | None
    actual_cost_usd: str | None
