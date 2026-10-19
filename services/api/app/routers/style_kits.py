"""Style kit endpoints (design §8.1: 스타일 키트·카테고리 팩)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.style import (
    StyleKitCreate,
    StyleKitRead,
    StyleKitUpdate,
)
from app.services import style_kit as style_service

router = APIRouter(tags=["style"])


@router.get("/projects/{project_id}/style-kits", response_model=list[StyleKitRead])
def list_style_kits(project_id: uuid.UUID, db: Session = Depends(get_db)) -> list[StyleKitRead]:
    return [StyleKitRead.model_validate(k) for k in style_service.list_style_kits(db, project_id)]


@router.post("/projects/{project_id}/style-kits", response_model=StyleKitRead, status_code=201)
def create_style_kit(
    project_id: uuid.UUID, body: StyleKitCreate, db: Session = Depends(get_db)
) -> StyleKitRead:
    kit = style_service.create_style_kit(
        db, name=body.name, project_id=project_id, fields=body.model_dump()
    )
    return StyleKitRead.model_validate(kit)


@router.patch("/style-kits/{kit_id}", response_model=StyleKitRead)
def update_style_kit(
    kit_id: uuid.UUID, body: StyleKitUpdate, db: Session = Depends(get_db)
) -> StyleKitRead:
    kit = style_service.update_style_kit(db, kit_id, body.model_dump(exclude_unset=True))
    return StyleKitRead.model_validate(kit)


@router.post("/style-kits/{kit_id}/lock", response_model=StyleKitRead)
def lock_style_kit(kit_id: uuid.UUID, db: Session = Depends(get_db)) -> StyleKitRead:
    return StyleKitRead.model_validate(style_service.lock_style_kit(db, kit_id))


@router.post("/style-kits/{kit_id}/duplicate", response_model=StyleKitRead, status_code=201)
def duplicate_style_kit(kit_id: uuid.UUID, db: Session = Depends(get_db)) -> StyleKitRead:
    return StyleKitRead.model_validate(style_service.duplicate_style_kit(db, kit_id))


# --- P1-7: style extraction (design §8.1) ---
from app.schemas.style_extract import StyleExtractAcceptedResponse, StyleExtractRequest
from app.services import idempotency, style_extract as extract_service


@router.post("/style-kits/{kit_id}/extract", response_model=StyleExtractAcceptedResponse, status_code=202)
def extract_style(
    kit_id: uuid.UUID,
    body: StyleExtractRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> StyleExtractAcceptedResponse:
    route = "POST /style-kits/{kit_id}/extract"
    store = idempotency.get_store()
    key = store.make_key(route, idempotency_key) if idempotency_key else None
    if key is not None:
        hit = store.recall(key)
        if hit is not None:
            return StyleExtractAcceptedResponse.model_validate(hit)
    result = extract_service.request_extract(
        db, kit_id, source_asset_ids=body.source_asset_ids
    )
    response = StyleExtractAcceptedResponse.model_validate(result)
    if key is not None:
        store.remember(key, response.model_dump(mode="json"))
    return response
