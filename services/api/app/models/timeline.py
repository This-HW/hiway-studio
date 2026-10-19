"""Timelines (design report §4.15). Export presets live in models/system.py
(§4.19), referenced here only by preset_id.

Timelines are assembled from shot clips (F-12). The cut list is the
normalized `timeline_clips` table (ADR-0007); the API exposes it as the
F-12 `clip_order` array but never stores it as JSON (D-12).
While `status='rendering'` the timeline is read-only (409 RENDER_LOCKED);
Phase 4 owns the QC gate (ADR-0006).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Timeline(Base):
    __tablename__ = "timelines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, default="기본 타임라인")
    # Contract enum subtitle_mode: none | single | dual.
    subtitle_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="single")
    # Track ids referenced by the render (Phase 4 creates the tables).
    # JSON list of UUID strings (portable across PostgreSQL/SQLite).
    subtitle_track_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    output_preset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("export_presets.id"), nullable=True
    )
    # Relative path only, e.g. "projects/<uuid>/timelines/<uuid>/output.mp4".
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Contract enum render_status: draft | rendering | done | failed.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    # Content version: bumped by every edit that affects the render output
    # (cut list, audio clips, subtitle cues/style/mode, track selection).
    # QC reports record the version they ran against; a report whose version
    # no longer matches is stale and cannot gate a render (design §10.4).
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TimelineClip(Base):
    """One entry of a timeline's cut list (ADR-0007, D-12).

    The storage truth for the F-12 `clip_order`; `position` is the 0-based
    order within the timeline. Time unit is seconds (float, D-27).
    """

    __tablename__ = "timeline_clips"
    __table_args__ = (
        UniqueConstraint("timeline_id", "position", name="uq_timeline_clips_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    timeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    in_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    out_sec: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
