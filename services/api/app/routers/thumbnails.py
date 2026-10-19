"""Thumbnail routes (P1-9, design §8.1).

`POST /projects/{id}/thumbnails` generates a batch of video thumbnails.
Title text is composited as a post-processing overlay (§10.5) — the image
model is instructed not to render text. Accepts an optional `Idempotency-Key`
header (design §12.2).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.thumbnail import ThumbnailAcceptedResponse, ThumbnailRequest
from app.services import idempotency, thumbnail as thumbnail_service
from app.services.thumbnail import DEFAULT_COUNT

router = APIRouter(tags=["thumbnails"])


@router.post("/projects/{project_id}/thumbnails", response_model=ThumbnailAcceptedResponse, status_code=202)
def create_thumbnails(
    project_id: uuid.UUID,
    body: ThumbnailRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> ThumbnailAcceptedResponse:
    route = "POST /projects/{project_id}/thumbnails"
    store = idempotency.get_store()
    key = store.make_key(route, idempotency_key) if idempotency_key else None
    if key is not None:
        hit = store.recall(key)
        if hit is not None:
            return ThumbnailAcceptedResponse.model_validate(hit)
    count = body.count if body.count is not None else DEFAULT_COUNT
    result = thumbnail_service.request_thumbnails(
        db,
        project_id,
        timeline_id=body.timeline_id,
        title_text=body.title_text,
        count=count,
    )
    response = ThumbnailAcceptedResponse.model_validate(result)
    if key is not None:
        store.remember(key, response.model_dump(mode="json"))
    return response
