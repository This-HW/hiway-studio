"""Camera presets (P1-2, design report P1-2 camera preset + 180-degree check).

Built-in presets are seeded idempotently by services/camera.py
(`ensure_builtin_camera_presets`, called from `python -m app.seed`).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CameraPreset(Base):
    """Reusable camera setup: lens + movement + angle.

    Built-in presets are immutable from the API (name is the business key).
    """

    __tablename__ = "camera_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lens: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "35mm"
    # Contract enum camera_movement (contracts/enums.json).
    movement: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Contract enum camera_angle (contracts/enums.json).
    angle: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Extra generation hints (e.g. aperture, stabilization) — free-form.
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_builtin: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"CameraPreset({self.name})"
