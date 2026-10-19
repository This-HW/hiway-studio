"""Previz schemas (P2 3D camera previz, ADR-0010).

Coordinate system: meters, Y-up. Documented on the model; repeated here
because API consumers see schemas, not models.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class Vec3(BaseModel):
    """Point in meters, Y-up coordinate system."""

    x: float
    y: float
    z: float


class CameraKeyframeCreate(BaseModel):
    # Seconds into the shot; must be >= 0 (service rejects negatives).
    t_sec: float = Field(ge=0)
    position: Vec3
    target: Vec3
    # Vertical field of view in degrees.
    fov_deg: float = Field(default=40.0, gt=0, lt=180)


class CameraKeyframeUpdate(BaseModel):
    t_sec: float | None = Field(default=None, ge=0)
    position: Vec3 | None = None
    target: Vec3 | None = None
    fov_deg: float | None = Field(default=None, gt=0, lt=180)


class CameraKeyframeRead(BaseModel):
    id: uuid.UUID
    shot_id: uuid.UUID
    t_sec: float
    position: Vec3
    target: Vec3
    fov_deg: float
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CameraSample(BaseModel):
    """One interpolated point on the camera path."""

    t_sec: float
    position: Vec3
    target: Vec3
    fov_deg: float


class CameraPathRead(BaseModel):
    shot_id: uuid.UUID
    keyframe_count: int
    samples: list[CameraSample]
