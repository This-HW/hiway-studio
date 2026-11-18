"""Thumbnail routes (P1-9, design §8.1).

`POST /projects/{id}/thumbnails` generates a batch of video thumbnails.
Title text is composited as a post-processing overlay (§10.5) — the image
model is instructed not to render text. Accepts an optional `Idempotency-Key`
header (design §12.2).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
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
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    count = body.count if body.count is not None else DEFAULT_COUNT
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=ThumbnailAcceptedResponse,
        create=lambda: thumbnail_service.request_thumbnails(
            db,
            project_id,
            timeline_id=body.timeline_id,
            title_text=body.title_text,
            count=count,
        ),
    )
