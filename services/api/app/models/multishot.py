"""Multishot coverage sets (design report §4.18, F-09, P1-1).

A multishot set fans one approved base frame out into six angle-specific
shots (establishing, wide, medium, medium_closeup, closeup, insert).
Each angle becomes a real Shot row (inserted right after the base shot)
plus one text_to_image generation; the multishot_frames rows link them
together so the set can be aggregated and queried as a unit.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The six coverage angles created by POST /shots/{id}/multishot.
COVERAGE_ANGLES: tuple[str, ...] = (
    "establishing",
    "wide",
    "medium",
    "medium_closeup",
    "closeup",
    "insert",
)

# Contract enum multishot_status (§6.4).
MULTISHOT_STATUSES: tuple[str, ...] = ("queued", "running", "partial", "done", "failed")


class MultishotSet(Base):
    __tablename__ = "multishot_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The base shot (must be frame_approved at request time).
    shot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The approved frame job the coverage is generated from.
    source_frame_generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("generations.id"), nullable=False
    )
    # Scene-state lock at creation: resolved style kit (id+version), character
    # bindings, scene context (location/time/mood/lighting), model id.
    state_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # Last broker dispatch of q.p1.run_multishot (reconcile clock while the
    # set is still queued, ADR-0013 / plan D9).
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MultishotFrame(Base):
    __tablename__ = "multishot_frames"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    multishot_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("multishot_sets.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # One of COVERAGE_ANGLES.
    coverage_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # The per-angle frame generation job (parent worker creates it).
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generations.id"), nullable=True
    )
    # The coverage shot inserted into the scene (shots.coverage_source_frame_id
    # points back at this row).
    shot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True
    )
    # pending | done | failed.
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # Per-cut approval.
    approved: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Design §4.18: one frame row per coverage angle in a set.
        UniqueConstraint(
            "multishot_set_id", "coverage_type", name="uq_multishot_frame_angle"
        ),
        # Plan D9 (RH2): a frame links to exactly one child shot/generation and
        # no two frames share one — a duplicate fan-out cannot re-link rows.
        UniqueConstraint("generation_id", name="uq_multishot_frame_generation"),
        UniqueConstraint("shot_id", name="uq_multishot_frame_shot"),
    )
