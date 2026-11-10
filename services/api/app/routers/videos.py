"""Shot video-generation routes (F-10, P0-13, design §8.1).

`POST /shots/{shot_id}/video` accepts an optional `Idempotency-Key` header
(design §12.2). I2V requires an approved frame: unapproved shots get
409 APPROVAL_REQUIRED (the review screen mirrors this by disabling the
clip button).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.video import VideoAcceptedResponse, VideoGenerateRequest, VideoRead
from app.services import idempotency, videos as video_service

router = APIRouter(tags=["videos"])


@router.post("/shots/{shot_id}/video", response_model=VideoAcceptedResponse, status_code=202)
def generate_video(
    shot_id: uuid.UUID,
    body: VideoGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=VideoAcceptedResponse,
        create=lambda: video_service.enqueue_video(
            db,
            shot_id,
            mode=body.mode,
            motion_prompt=body.motion_prompt,
            duration_sec=body.duration_sec,
            model_id=body.model_id,
        ),
    )


@router.get("/shots/{shot_id}/videos", response_model=list[VideoRead])
def list_videos(shot_id: uuid.UUID, db: Session = Depends(get_db)) -> list[VideoRead]:
    return [VideoRead.model_validate(v) for v in video_service.list_videos(db, shot_id)]
