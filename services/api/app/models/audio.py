"""Audio tables (design report §4.14, P0 audio scope).

P0 scope: audio clip upload + timeline placement (BGM/SFX). TTS, voice
cloning, emotion weights and dubbing lip-sync are P1-5 and are NOT
implemented here — the `AudioGeneration` rows P0 creates are `status='done'`
wrappers around uploaded files (kind bgm|sfx).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AudioGeneration(Base):
    __tablename__ = "audio_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    # Contract enum audio_kind: dialogue | narration | bgm | sfx.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotion_weights: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Worker hints (Phase 5): e.g. {"target_duration_sec": 3.2} for lipsync
    # fitting, {"_retry_count": n} for the worker's own retry bookkeeping.
    # Provider-facing TTS params (text, voice_id, emotion_weights) stay in
    # their documented columns / the adapter payload, not here.
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Relative path only (projects/...), like every other output path.
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Contract enum job_status (shared with generations).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    # --- execution ownership (ADR-0013, plan D4/D7); same contract as
    # generations. Unused for uploaded bgm/sfx rows (status 'done' at insert).
    exec_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submit_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    provider_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Automatic retries consumed (was params["_retry_count"]) and executions
    # claimed (audio has no attempts table).
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Budget period anchor (B5 review H1): set when the row is created and
    # again on every manual retry, so a retry approved this month stays in
    # this month's ledger even when the row was created earlier.
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TimelineAudioClip(Base):
    """Placement of one audio row on a timeline (design §4.14)."""

    __tablename__ = "timeline_audio_clips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    timeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    audio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("audio_generations.id", ondelete="CASCADE"), nullable=False
    )
    # Contract enum audio_kind, mirrors audio_generations.kind (track lane).
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Timeline position in seconds (float, D-27).
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    # NULL = use the full source audio length.
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    gain_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
