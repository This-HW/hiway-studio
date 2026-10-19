"""Previz routes (P2 3D camera previz, ADR-0010).

GET/POST /shots/{id}/camera-keyframes — keyframe list & create (free, no job).
PATCH/DELETE /camera-keyframes/{kf_id} — update & delete.
GET /shots/{id}/camera-path — linearly interpolated camera path.

Coordinate system: meters, Y-up. This is a camera-move planning aid;
the web viewer draws the path on Canvas 2D — no 3D rendering here.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import previz as s
from app.services import previz as previz_service

router = APIRouter(tags=["previz"])


@router.get("/shots/{shot_id}/camera-keyframes",
            response_model=list[s.CameraKeyframeRead])
def list_keyframes(shot_id: uuid.UUID, db: Session = Depends(get_db)):
    return previz_service.list_keyframes(db, shot_id)


@router.post("/shots/{shot_id}/camera-keyframes",
             response_model=s.CameraKeyframeRead, status_code=201)
def create_keyframe(shot_id: uuid.UUID, body: s.CameraKeyframeCreate,
                    db: Session = Depends(get_db)):
    kf = previz_service.create_keyframe(
        db, shot_id,
        t_sec=body.t_sec,
        position=body.position.model_dump(),
        target=body.target.model_dump(),
        fov_deg=body.fov_deg,
    )
    db.commit()
    return kf


@router.patch("/camera-keyframes/{kf_id}", response_model=s.CameraKeyframeRead)
def update_keyframe(kf_id: uuid.UUID, body: s.CameraKeyframeUpdate,
                    db: Session = Depends(get_db)):
    kf = previz_service.update_keyframe(
        db, kf_id,
        t_sec=body.t_sec,
        position=body.position.model_dump() if body.position else None,
        target=body.target.model_dump() if body.target else None,
        fov_deg=body.fov_deg,
    )
    db.commit()
    return kf


@router.delete("/camera-keyframes/{kf_id}", status_code=204)
def delete_keyframe(kf_id: uuid.UUID, db: Session = Depends(get_db)):
    previz_service.delete_keyframe(db, kf_id)
    db.commit()
    return None


@router.get("/shots/{shot_id}/camera-path", response_model=s.CameraPathRead)
def camera_path(shot_id: uuid.UUID,
                samples: int = Query(default=50, ge=2, le=200),
                db: Session = Depends(get_db)):
    path = previz_service.camera_path(db, shot_id, samples=samples)
    return s.CameraPathRead(
        shot_id=path["shot_id"],
        keyframe_count=path["keyframe_count"],
        samples=[s.CameraSample(**sm) for sm in path["samples"]],
    )
