"""Subtitle tables (design report §4.16, P0-15).

The SRT/VTT text is the canonical subtitle truth (G-09); burn-in happens
only at render time (D-14). A cue overlap inside one track is rejected by
the API with 422 SUBTITLE_OVERLAP (§4.16).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SubtitleTrack(Base):
    __tablename__ = "subtitle_tracks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)  # BCP 47, e.g. "ko"
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_default: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SubtitleCue(Base):
    __tablename__ = "subtitle_cues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("subtitle_tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Seconds (float, D-27). CHECK(start_sec > 0) and CHECK(end_sec > start_sec)
    # live in the migration; the service layer re-validates for clear errors.
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    word_timings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    style_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubtitleStyle(Base):
    """Subtitle visual style. `track_id IS NULL` = project default style.

    Defaults follow design §4.16 ([assumption] font/size).
    """

    __tablename__ = "subtitle_styles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("subtitle_tracks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    font_family: Mapped[str] = mapped_column(Text, nullable=False, default="Noto Sans KR")
    font_size_px: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
    color: Mapped[str] = mapped_column(Text, nullable=False, default="#FFFFFF")
    # Contract enum subtitle_position: top | middle | bottom.
    position: Mapped[str] = mapped_column(String(16), nullable=False, default="bottom")
    background_color: Mapped[str | None] = mapped_column(Text, nullable=True)
    background_opacity: Mapped[float] = mapped_column(Float, nullable=False, default=0.6)
    line_break_rule: Mapped[str] = mapped_column(Text, nullable=False, default="auto")
    safe_area_margin_pct: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
