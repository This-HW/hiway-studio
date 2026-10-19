"""Generation jobs (design report §4.11) and attempt history (§4.12).

Every generation request is one row here; the row is the queue job record and
the Celery task receives only the generation id (ARCHITECTURE §8).

Note: model_id stays nullable in Phase 1. The design marks it NN, but the
worker routes the model at execution time when the service has not chosen one
yet; the Phase-0 completion test also creates rows without a model. Phase 2
(cost tracking) will enforce NN.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Contract enum job_type.
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text_to_image")
    # NULL for generations unrelated to a shot (character masters, sheets...).
    shot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    batch_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_batch: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Contract enum job_status (§6.1).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    progress_pct: Mapped[float] = mapped_column(nullable=False, default=0.0)
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("models.model_id"), nullable=True, index=True
    )
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_final: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_final: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Model params + router metadata (router_reason, fallback_chain, auto_fallback)
    # + reserved "_"-prefixed keys for pipeline links (see services/character_pipeline).
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reference_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    input_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Relative path only (e.g. "projects/<uuid>/frames/<uuid>_0.png"). Never absolute.
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(nullable=True)
    style_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("style_kits.id"), nullable=True
    )
    style_kit_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    auto_fallback: Mapped[bool] = mapped_column(nullable=False, default=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"Generation({self.id}, {self.job_type}, {self.status})"


class GenerationAttempt(Base):
    """Per-attempt history for retries and fallbacks (design §4.12)."""

    __tablename__ = "generation_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("generations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Video(Base):
    """Video clip artifact of a shot (design report §4.13).

    A shot may have several clips (history); timelines reference `video_id`.
    The worker (tasks_video) owns the generating -> done/failed transitions.
    """

    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    shot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generations.id"), nullable=True
    )
    # Contract enum video_mode: i2v | t2v.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # Motion prompt: describe only what must move/change (§5.9 rule).
    motion_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float] = mapped_column(nullable=False)
    fps: Mapped[int] = mapped_column(nullable=False, default=24)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Relative path only, e.g. "projects/<uuid>/scenes/<uuid>/shots/<uuid>/videos/<uuid>.mp4".
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    # Contract enum video_status: draft | generating | done | failed.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
