"""Camera keyframes for 3D previz (P2, ADR-0010).

Coordinate system: meters, Y-up. `position`/`target` are stored as JSON
{x, y, z} dicts so the schema stays free of per-axis columns.
UNIQUE(shot_id, t_sec) — one keyframe per time point within a shot.

This is a previz planning aid only: the viewer interpolates keyframes
with plain linear math; there is no scene geometry or lighting model.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, UniqueConstraint, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CameraKeyframe(Base):
    __tablename__ = "camera_keyframes"
    __table_args__ = (
        UniqueConstraint("shot_id", "t_sec", name="uq_camera_keyframe_shot_t"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    shot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Seconds into the shot. >= 0 (enforced in the service layer).
    t_sec: Mapped[float] = mapped_column(Float, nullable=False)
    # Camera position in meters, Y-up: {"x": .., "y": .., "z": ..}.
    position: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Look-at target in meters, Y-up: {"x": .., "y": .., "z": ..}.
    target: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Vertical field of view in degrees.
    fov_deg: Mapped[float] = mapped_column(Float, nullable=False, default=40.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
