"""Prompt variation routes (P1-3).

POST /shots/{shot_id}/frame/variations creates N variant batches for one
shot (202 + estimated_cost_usd). Job-creating: budget guard (inside the
service) + Idempotency-Key (services.idempotency, design §12.2, plan D2).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.camera import VariationsRequest, VariationsResponse
from app.services import generations as generation_service
from app.services import idempotency

router = APIRouter(tags=["variations"])


@router.post(
    "/shots/{shot_id}/frame/variations",
    response_model=VariationsResponse,
    status_code=202,
)
def create_variations(
    shot_id: uuid.UUID,
    body: VariationsRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    # Same idempotency rules as the other job-creating routes (design
    # §12.2, plan D2); the full VariationsResponse body is replayed.
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=VariationsResponse,
        create=lambda: generation_service.enqueue_variations(
            db,
            shot_id,
            count=body.count,
            variation_axis=body.variation_axis,
            model_id=body.model_id,
            seed=body.seed,
        ),
    )
