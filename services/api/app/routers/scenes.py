"""Scene / shot / shot-reference routes (P0-9, design §8.1).

Everything composes services/scene.py, services/approval.py and
services/continuity.py; this module holds no business logic.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.production import (
    SceneCreate,
    SceneRead,
    SceneUpdate,
    ShotCreate,
    ShotRead,
    ShotReferenceCreate,
    ShotReferenceRead,
    ShotReorderRequest,
    ShotUpdate,
)
from app.schemas.generation import ApproveRequest, ContinuityResponse
from app.schemas.camera import CameraCheckResponse
from app.services import approval as approval_service
from app.services import camera as camera_service
from app.services import continuity as continuity_service
from app.services import scene as scene_service

router = APIRouter(tags=["scenes"])


def _shot_card(db: Session, shot) -> ShotRead:
    return ShotRead.model_validate(scene_service.shot_card(db, shot))


def _scene_read(db: Session, scene) -> SceneRead:
    payload = SceneRead.model_validate(scene)
    payload.shots = [
        _shot_card(db, shot) for shot in scene_service.list_shots(db, scene.id)
    ]
    return payload


@router.get("/projects/{project_id}/scenes", response_model=list[SceneRead])
def list_scenes(project_id: uuid.UUID, db: Session = Depends(get_db)) -> list[SceneRead]:
    return [_scene_read(db, s) for s in scene_service.list_scenes(db, project_id)]


@router.post("/projects/{project_id}/scenes", response_model=SceneRead, status_code=201)
def create_scene(
    project_id: uuid.UUID, body: SceneCreate, db: Session = Depends(get_db)
) -> SceneRead:
    scene = scene_service.create_scene(db, project_id, body.model_dump())
    return _scene_read(db, scene)


@router.get("/scenes/{scene_id}", response_model=SceneRead)
def get_scene(scene_id: uuid.UUID, db: Session = Depends(get_db)) -> SceneRead:
    return _scene_read(db, scene_service.get_scene(db, scene_id))


@router.patch("/scenes/{scene_id}", response_model=SceneRead)
def update_scene(
    scene_id: uuid.UUID, body: SceneUpdate, db: Session = Depends(get_db)
) -> SceneRead:
    scene = scene_service.update_scene(
        db, scene_id, body.model_dump(exclude_unset=True)
    )
    return _scene_read(db, scene)


@router.delete("/scenes/{scene_id}", status_code=204)
def delete_scene(scene_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    scene_service.delete_scene(db, scene_id)


@router.post("/scenes/{scene_id}/shots", response_model=ShotRead, status_code=201)
def create_shot(
    scene_id: uuid.UUID, body: ShotCreate, db: Session = Depends(get_db)
) -> ShotRead:
    return _shot_card(db, scene_service.create_shot(db, scene_id, body.model_dump()))


@router.get("/scenes/{scene_id}/shots", response_model=list[ShotRead])
def list_shots(scene_id: uuid.UUID, db: Session = Depends(get_db)) -> list[ShotRead]:
    return [_shot_card(db, s) for s in scene_service.list_shots(db, scene_id)]


@router.post("/scenes/{scene_id}/shots/reorder", response_model=list[ShotRead])
def reorder_shots(
    scene_id: uuid.UUID, body: ShotReorderRequest, db: Session = Depends(get_db)
) -> list[ShotRead]:
    shots = scene_service.reorder_shots(db, scene_id, body.ordered_ids)
    return [_shot_card(db, s) for s in shots]


@router.get("/shots/{shot_id}", response_model=ShotRead)
def get_shot(shot_id: uuid.UUID, db: Session = Depends(get_db)) -> ShotRead:
    return _shot_card(db, scene_service.get_shot(db, shot_id))


@router.patch("/shots/{shot_id}", response_model=ShotRead)
def update_shot(
    shot_id: uuid.UUID, body: ShotUpdate, db: Session = Depends(get_db)
) -> ShotRead:
    shot = scene_service.update_shot(db, shot_id, body.model_dump(exclude_unset=True))
    return _shot_card(db, shot)


@router.delete("/shots/{shot_id}", status_code=204)
def delete_shot(shot_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    scene_service.delete_shot(db, shot_id)


@router.post("/shots/{shot_id}/duplicate", response_model=ShotRead, status_code=201)
def duplicate_shot(shot_id: uuid.UUID, db: Session = Depends(get_db)) -> ShotRead:
    return _shot_card(db, scene_service.duplicate_shot(db, shot_id))


@router.get("/shots/{shot_id}/references", response_model=list[ShotReferenceRead])
def list_references(shot_id: uuid.UUID, db: Session = Depends(get_db)):
    return [
        ShotReferenceRead.model_validate(scene_service.reference_card(db, r))
        for r in scene_service.list_references(db, shot_id)
    ]


@router.post("/shots/{shot_id}/references", response_model=ShotReferenceRead, status_code=201)
def add_reference(
    shot_id: uuid.UUID, body: ShotReferenceCreate, db: Session = Depends(get_db)
):
    ref = scene_service.add_reference(
        db,
        shot_id,
        character_id=body.character_id,
        asset_id=body.asset_id,
        purpose=body.purpose,
        reference_detail_id=body.reference_detail_id,
    )
    return ShotReferenceRead.model_validate(scene_service.reference_card(db, ref))


@router.delete("/references/{reference_id}", status_code=204)
def delete_shot_reference(reference_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    scene_service.delete_reference(db, reference_id)


@router.get("/scenes/{scene_id}/camera-check", response_model=CameraCheckResponse)
def camera_check(scene_id: uuid.UUID, db: Session = Depends(get_db)) -> CameraCheckResponse:
    """P1-2 180-degree rule check for the shot board (warnings, not errors)."""
    return CameraCheckResponse(
        scene_id=scene_id,
        violations=camera_service.check_camera_axis(db, scene_id),
    )


@router.post("/shots/{shot_id}/frame/approve", response_model=ShotRead)
def approve_frame(
    shot_id: uuid.UUID, body: ApproveRequest, db: Session = Depends(get_db)
) -> ShotRead:
    shot = approval_service.approve_frame(db, shot_id, body.generation_id)
    return _shot_card(db, shot)


@router.get("/shots/{shot_id}/continuity", response_model=ContinuityResponse)
def get_continuity(shot_id: uuid.UUID, db: Session = Depends(get_db)) -> ContinuityResponse:
    return ContinuityResponse.model_validate(
        continuity_service.get_continuity_source(db, shot_id)
    )
