"""Prompt variation routes (P1-3).

POST /shots/{shot_id}/frame/variations creates N variant batches for one
shot (202 + estimated_cost_usd). Job-creating: budget guard (inside the
service) + Idempotency-Key (60s window, design §12.2).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.camera import VariationsRequest, VariationsResponse
from app.services import generations as generation_service
from app.services.idempotency import get_store

router = APIRouter(tags=["variations"])


@router.post(
    "/shots/{shot_id}/frame/variations",
    response_model=VariationsResponse,
    status_code=202,
)
def create_variations(
    shot_id: uuid.UUID,
    body: VariationsRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> VariationsResponse:
    # Same 60s idempotency window as the other job-creating routes
    # (design §12.2); the full VariationsResponse body is replayed.
    store = get_store()
    store_key = (
        store.make_key("POST /shots/{shot_id}/frame/variations", idempotency_key)
        if idempotency_key
        else None
    )
    if store_key is not None:
        hit = store.recall(store_key)
        if hit is not None:
            return VariationsResponse.model_validate(hit)
    result = generation_service.enqueue_variations(
        db,
        shot_id,
        count=body.count,
        variation_axis=body.variation_axis,
        model_id=body.model_id,
        seed=body.seed,
    )
    response = VariationsResponse.model_validate(result)
    if store_key is not None:
        store.remember(store_key, response.model_dump(mode="json"))
    return response
