"""Multishot coverage routes (P1-1, design §8.1).

`POST /shots/{shot_id}/multishot` accepts an optional `Idempotency-Key` header
(design §12.2). The base shot must have an approved frame
(409 APPROVAL_REQUIRED otherwise).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.multishot import (
    MultishotAcceptedResponse,
    MultishotFrameRead,
    MultishotRequest,
    MultishotSetRead,
)
from app.services import idempotency, multishot as multishot_service

router = APIRouter(tags=["multishots"])


@router.post("/shots/{shot_id}/multishot", response_model=MultishotAcceptedResponse, status_code=202)
def create_multishot(
    shot_id: uuid.UUID,
    body: MultishotRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=MultishotAcceptedResponse,
        create=lambda: multishot_service.request_coverage(db, shot_id, model_id=body.model_id),
    )


@router.get("/multishot-sets/{set_id}", response_model=MultishotSetRead)
def get_multishot_set(set_id: uuid.UUID, db: Session = Depends(get_db)) -> MultishotSetRead:
    mset = multishot_service.get_set(db, set_id)
    frames = multishot_service.list_frames(db, set_id)
    return MultishotSetRead(
        id=mset.id,
        shot_id=mset.shot_id,
        status=mset.status,
        frames=[MultishotFrameRead.model_validate(f) for f in frames],
    )
