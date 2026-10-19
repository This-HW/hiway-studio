"""Camera preset routes (P1-2). Thin: service -> respond."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.camera import CameraPresetRead
from app.services import camera as camera_service

router = APIRouter(tags=["camera-presets"])


@router.get("/camera-presets", response_model=list[CameraPresetRead])
def list_camera_presets(db: Session = Depends(get_db)) -> list[CameraPresetRead]:
    return [CameraPresetRead.model_validate(p) for p in camera_service.list_presets(db)]
