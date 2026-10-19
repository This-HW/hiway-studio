"""Model registry endpoints (design §8.1)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import model_registry as registry_service

router = APIRouter(tags=["models"])


class ModelUpdate(BaseModel):
    status: str | None = None  # model_status: active|disabled|deprecated
    fallback_model_ids: list[str] | None = None


@router.get("/models")
def list_models(
    modality: str | None = Query(default=None),
    status: str = Query(default="active"),
    db: Session = Depends(get_db),
) -> list[dict]:
    return registry_service.list_models(db, modality, status)


@router.get("/models/{model_id}")
def get_model(model_id: str, db: Session = Depends(get_db)) -> dict:
    return registry_service.get_model(db, model_id)


@router.patch("/models/{model_id}")
def update_model(model_id: str, body: ModelUpdate, db: Session = Depends(get_db)) -> dict:
    """Operator edit: enable/disable a model, change its fallback chain."""
    return registry_service.update_model(
        db,
        model_id,
        status=body.status,
        fallback_model_ids=body.fallback_model_ids,
    )
