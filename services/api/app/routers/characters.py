"""Character endpoints (G-6, D-18, D-19)."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.db.session import get_db
from app.schemas.character import (
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    JobAcceptedResponse,
    MasterGenerateRequest,
    ReferenceCreate,
    ReferenceRead,
    ReferenceUpdate,
    SheetGenerateRequest,
    SheetGenerateResponse,
    WardrobeCreate,
    WardrobeRead,
)
from app.services import character as character_service
from app.services import character_pipeline

router = APIRouter(tags=["characters"])


def _to_read(character, db) -> CharacterRead:
    payload = CharacterRead.model_validate(character)
    payload.master_thumbnail = character_service.get_master_thumbnail(db, character.id)
    return payload


@router.post("/projects/{project_id}/characters", response_model=CharacterRead, status_code=201)
def create_character(
    project_id: uuid.UUID, body: CharacterCreate, db: Session = Depends(get_db)
) -> CharacterRead:
    character = character_service.create_character(
        db,
        name=body.name,
        appearance_prompt=body.appearance_prompt,
        description=body.description,
        negative_prompt=body.negative_prompt,
        consistency_method=body.consistency_method,
        default_strength=body.default_strength,
        style_notes=body.style_notes,
        tags=body.tags,
        project_id=project_id,
    )
    return _to_read(character, db)


@router.post("/characters", response_model=CharacterRead, status_code=201)
def create_global_character(body: CharacterCreate, db: Session = Depends(get_db)) -> CharacterRead:
    character = character_service.create_character(
        db,
        name=body.name,
        appearance_prompt=body.appearance_prompt,
        description=body.description,
        negative_prompt=body.negative_prompt,
        consistency_method=body.consistency_method,
        default_strength=body.default_strength,
        style_notes=body.style_notes,
        tags=body.tags,
        project_id=None,
    )
    return _to_read(character, db)


@router.get("/projects/{project_id}/characters", response_model=list[CharacterRead])
def list_characters(
    project_id: uuid.UUID,
    scope: Literal["project", "global", "all"] = Query(default="all"),
    db: Session = Depends(get_db),
) -> list[CharacterRead]:
    return [_to_read(c, db) for c in character_service.list_characters(db, project_id, scope)]


@router.get("/characters/{character_id}", response_model=CharacterRead)
def get_character(character_id: uuid.UUID, db: Session = Depends(get_db)) -> CharacterRead:
    return _to_read(character_service.get_character(db, character_id), db)


@router.patch("/characters/{character_id}", response_model=CharacterRead)
def update_character(
    character_id: uuid.UUID, body: CharacterUpdate, db: Session = Depends(get_db)
) -> CharacterRead:
    character = character_service.update_character(
        db, character_id, body.model_dump(exclude_unset=True)
    )
    return _to_read(character, db)


@router.delete("/characters/{character_id}", status_code=204)
def delete_character(character_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    character_service.delete_character(db, character_id)


@router.post("/characters/{character_id}/master",
             response_model=JobAcceptedResponse, status_code=202)
def generate_master(
    character_id: uuid.UUID,
    body: MasterGenerateRequest,
    db: Session = Depends(get_db),
) -> JobAcceptedResponse:
    character = character_service.get_character(db, character_id)
    if character.project_id is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Master generation for a global character needs a project context (Phase 2).",
        )
    job_id = character_pipeline.enqueue_master(
        db, character, body.prompt, body.model_id, character.project_id
    )
    return JobAcceptedResponse(job_id=job_id)


@router.post("/characters/{character_id}/sheet",
             response_model=SheetGenerateResponse, status_code=202)
def generate_sheet(
    character_id: uuid.UUID,
    body: SheetGenerateRequest,
    db: Session = Depends(get_db),
) -> SheetGenerateResponse:
    character = character_service.get_character(db, character_id)
    if character.project_id is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Sheet generation for a global character needs a project context (Phase 2).",
        )
    job_ids, estimated = character_pipeline.enqueue_sheet(
        db, character, character.project_id, body.views, body.expressions, body.outfits, body.poses
    )
    return SheetGenerateResponse(job_ids=job_ids, estimated_cost_usd=estimated)


@router.get("/characters/{character_id}/references", response_model=list[ReferenceRead])
def list_references(character_id: uuid.UUID, db: Session = Depends(get_db)) -> list[ReferenceRead]:
    return [
        ReferenceRead.model_validate(r)
        for r in character_service.list_references(db, character_id)
    ]


@router.post("/characters/{character_id}/references", response_model=ReferenceRead, status_code=201)
def add_reference(
    character_id: uuid.UUID, body: ReferenceCreate, db: Session = Depends(get_db)
) -> ReferenceRead:
    ref = character_service.add_reference(
        db, character_id, body.image_path, body.role, body.is_primary, body.source
    )
    return ReferenceRead.model_validate(ref)


@router.patch("/references/{reference_id}", response_model=ReferenceRead)
def update_reference(
    reference_id: uuid.UUID, body: ReferenceUpdate, db: Session = Depends(get_db)
) -> ReferenceRead:
    ref = character_service.update_reference(db, reference_id, body.role, body.is_primary)
    return ReferenceRead.model_validate(ref)


@router.delete("/references/{reference_id}", status_code=204)
def delete_reference(reference_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    character_service.delete_reference(db, reference_id)


@router.get("/characters/{character_id}/wardrobe", response_model=list[WardrobeRead])
def list_wardrobe(character_id: uuid.UUID, db: Session = Depends(get_db)) -> list[WardrobeRead]:
    return [
        WardrobeRead.model_validate(w)
        for w in character_service.list_wardrobe(db, character_id)
    ]


@router.post("/characters/{character_id}/wardrobe", response_model=WardrobeRead, status_code=201)
def create_wardrobe(
    character_id: uuid.UUID, body: WardrobeCreate, db: Session = Depends(get_db)
) -> WardrobeRead:
    wardrobe = character_service.create_wardrobe(
        db,
        character_id,
        body.name,
        body.prompt_fragment,
        body.reference_image_id,
        body.is_default,
    )
    return WardrobeRead.model_validate(wardrobe)


# --- P1-4: LoRA training (design §4.7 character_loras, §8.1) ---
from app.schemas.lora import LoraTrainAcceptedResponse, LoraModelRead, LoraTrainRequest
from app.services import idempotency, lora as lora_service


@router.post("/characters/{character_id}/lora/train", response_model=LoraTrainAcceptedResponse, status_code=202)
def train_lora(
    character_id: uuid.UUID,
    body: LoraTrainRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> LoraTrainAcceptedResponse:
    route = "POST /characters/{character_id}/lora/train"
    store = idempotency.get_store()
    key = store.make_key(route, idempotency_key) if idempotency_key else None
    if key is not None:
        hit = store.recall(key)
        if hit is not None:
            return LoraTrainAcceptedResponse.model_validate(hit)
    result = lora_service.request_train(
        db,
        character_id,
        training_image_ids=body.training_image_ids,
        rank=body.rank,
        steps=body.steps,
        trigger_word=body.trigger_word,
        confirm=body.confirm,
    )
    response = LoraTrainAcceptedResponse.model_validate(result)
    if key is not None:
        store.remember(key, response.model_dump(mode="json"))
    return response


@router.get("/characters/{character_id}/loras", response_model=list[LoraModelRead])
def list_loras(character_id: uuid.UUID, db: Session = Depends(get_db)) -> list[LoraModelRead]:
    return [LoraModelRead.model_validate(l) for l in lora_service.list_loras(db, character_id)]
