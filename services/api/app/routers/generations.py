"""Shot generation / job routes (F-06, F-07, P0-14, design §8.1).

Generation POSTs accept an optional `Idempotency-Key` header: within
defaults.IDEMPOTENCY_TTL_SEC a repeated key on the same request path replays
the original response instead of creating new jobs (design §12.2, plan D2,
services.idempotency).
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.generation import (
    BatchDetailResponse,
    FrameGenerateRequest,
    GenerationRead,
    InpaintRequest,
    JobAcceptedResponse,
    OutpaintRequest,
    RetakeRequest,
    UpscaleRequest,
)
from app.services import generations as generation_service
from app.services import idempotency

router = APIRouter(tags=["generations"])


@router.post("/shots/{shot_id}/frame", response_model=JobAcceptedResponse, status_code=202)
def generate_frame(
    shot_id: uuid.UUID,
    body: FrameGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=JobAcceptedResponse,
        create=lambda: generation_service.enqueue_frame(
            db,
            shot_id,
            model_id=body.model_id,
            batch_size=body.batch_size,
            overrides=body.overrides,
            use_continuity=body.use_continuity,
            seed=body.seed,
        ),
    )


@router.post("/shots/{shot_id}/retake", response_model=JobAcceptedResponse, status_code=202)
def retake(
    shot_id: uuid.UUID,
    body: RetakeRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=JobAcceptedResponse,
        create=lambda: generation_service.enqueue_retake(
            db,
            shot_id,
            model_id=body.model_id,
            batch_size=body.batch_size,
            overrides=body.overrides,
            seed=body.seed,
        ),
    )


@router.post("/shots/{shot_id}/inpaint", response_model=JobAcceptedResponse, status_code=202)
def inpaint(
    shot_id: uuid.UUID,
    body: InpaintRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=JobAcceptedResponse,
        create=lambda: generation_service.enqueue_inpaint(
            db,
            shot_id,
            generation_id=body.generation_id,
            mask_path=body.mask_path,
            prompt=body.prompt,
            model_id=body.model_id,
        ),
    )


@router.post("/shots/{shot_id}/upscale", response_model=JobAcceptedResponse, status_code=202)
def upscale(
    shot_id: uuid.UUID,
    body: UpscaleRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=JobAcceptedResponse,
        create=lambda: generation_service.enqueue_upscale(
            db, shot_id, generation_id=body.generation_id, model_id=body.model_id
        ),
    )


@router.post("/shots/{shot_id}/outpaint", response_model=JobAcceptedResponse, status_code=202)
def outpaint(
    shot_id: uuid.UUID,
    body: OutpaintRequest,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=JobAcceptedResponse,
        create=lambda: generation_service.enqueue_outpaint(
            db,
            shot_id,
            generation_id=body.generation_id,
            prompt=body.prompt,
            mask_path=body.mask_path,
            direction=body.direction,
            model_id=body.model_id,
        ),
    )


@router.get("/jobs", response_model=list[GenerationRead])
def list_jobs(
    project_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    shot_id: uuid.UUID | None = Query(default=None),
    model_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[GenerationRead]:
    rows = generation_service.list_jobs(
        db,
        project_id=project_id,
        status=status,
        job_type=job_type,
        shot_id=shot_id,
        model_id=model_id,
        limit=limit,
        offset=offset,
    )
    return [GenerationRead.model_validate(r) for r in rows]


@router.get(
    "/jobs/{job_id}",
    response_model=GenerationRead | BatchDetailResponse,
)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job, children = generation_service.get_job_with_children(db, job_id)
    if children is None:
        return GenerationRead.model_validate(job)
    return BatchDetailResponse(
        batch=GenerationRead.model_validate(job),
        children=[GenerationRead.model_validate(c) for c in children],
    )


@router.post("/jobs/{job_id}/retry", response_model=JobAcceptedResponse, status_code=202)
def retry_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobAcceptedResponse:
    return JobAcceptedResponse.model_validate(generation_service.retry_job(db, job_id))


@router.post("/jobs/{job_id}/cancel", status_code=200)
def cancel_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    return generation_service.cancel_job(db, job_id)
