"""Scenes, shots, shot references (design report §4.4–§4.6).

Tables are created in Phase 1 because the completion proof references a
global character from another project's shot (shot_references), and script
import (F-02) drafts scenes. The API routers (routers/scenes.py) arrive in
Phase 2 — no endpoints are exposed yet.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Uuid
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import flag_modified

from app.db.base import Base


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )
    beat_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("beats.id", ondelete="SET NULL"), nullable=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_of_day: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(Text, nullable=True)
    lighting: Mapped[str | None] = mapped_column(Text, nullable=True)
    style_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("style_kits.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Shot(Base):
    __tablename__ = "shots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scene_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Contract enums: shot_type, camera_angle, camera_movement (contracts/enums.json).
    shot_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    camera_angle: Mapped[str | None] = mapped_column(String(24), nullable=True)
    movement: Mapped[str | None] = mapped_column(String(24), nullable=True)
    duration_sec: Mapped[float] = mapped_column(nullable=False, default=5.0)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotion_weights: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sfx_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    intended_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P1-3: pinned seed for reproducible frame generation. When set, /frame and
    # /retake reuse it unless an explicit seed is passed (which re-pins it).
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # P1-2: reusable camera setup. NULL = free-form (shot_type/camera_angle/movement).
    camera_preset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("camera_presets.id", ondelete="SET NULL"), nullable=True
    )
    # P1-2: 180-degree rule — which side of the action axis this shot is on.
    # left | right | center (NULL = unknown, skipped by the check).
    camera_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Contract enum shot_status (§6.2).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    approved_frame_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generations.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frame_stale: Mapped[bool] = mapped_column(nullable=False, default=False)
    # B4R4-M1: +1 on every invalidation (mark_frame_stale), also when the shot
    # is already stale. An enqueue clears frame_stale only when this is still
    # the value it saw when it assembled the prompt (generations).
    frame_stale_rev: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    continuity_source_shot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shots.id"), nullable=True
    )
    style_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("style_kits.id"), nullable=True
    )
    # FK to multishot_frames is added in Phase 5 (table does not exist yet).
    coverage_source_frame_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def mark_frame_stale(self) -> None:
        """Invalidate the shot's generated frames (F-06, design §8.1).

        The single way to set frame_stale=True: it also bumps frame_stale_rev.
        On a persisted row the bump is the SQL expression `frame_stale_rev + 1`
        (evaluated by the UPDATE on the current row, so concurrent
        invalidations never collapse into one value)."""
        self.frame_stale = True
        if sa_inspect(self).persistent:
            # Always write frame_stale in the same UPDATE as the rev bump: if this
            # session already loaded True, plain assignment is not a net change and
            # the ORM would omit it, letting a concurrent enqueue's False survive
            # (review R5-M1).
            flag_modified(self, "frame_stale")
            self.frame_stale_rev = type(self).frame_stale_rev + 1
        else:
            self.frame_stale_rev = (self.frame_stale_rev or 0) + 1


class ShotReference(Base):
    __tablename__ = "shot_references"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    shot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("characters.id", ondelete="CASCADE"), nullable=True
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("assets.id", ondelete="CASCADE"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_detail_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # G-03: exactly one of character_id / asset_id must be non-null.
        CheckConstraint(
            "(CASE WHEN character_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN asset_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_shot_references_exactly_one_target",
        ),
    )
