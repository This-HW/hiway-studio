"""Timeline routes (F-12, F-15, P0-14/18, design §8.1).

While a timeline is `rendering` it is read-only: PATCH and build-from-shots
raise 409 RENDER_LOCKED (the UI locks the editor until `render.completed`).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.video import (
    RenderAcceptedResponse,
    RenderRequest,
    TimelineCreate,
    TimelineRead,
    TimelineUpdate,
)
from app.services import file_serve, render as render_service
from app.services import timelines as timeline_service

router = APIRouter(tags=["timelines"])


def _read(db: Session, timeline) -> TimelineRead:
    """TimelineRead with clip_order assembled from timeline_clips (ADR-0007)."""
    data = TimelineRead.model_validate(timeline).model_dump()
    data["clip_order"] = timeline_service.get_clip_order(db, timeline.id)
    return TimelineRead.model_validate(data)


@router.get("/projects/{project_id}/timelines", response_model=list[TimelineRead])
def list_timelines(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return [
        _read(db, t) for t in timeline_service.list_timelines(db, project_id)
    ]


@router.post("/projects/{project_id}/timelines", response_model=TimelineRead,
             status_code=201)
def create_timeline(
    project_id: uuid.UUID, body: TimelineCreate, db: Session = Depends(get_db)
):
    timeline = timeline_service.create_timeline(db, project_id, name=body.name)
    db.commit()
    return _read(db, timeline)


@router.get("/timelines/{timeline_id}", response_model=TimelineRead)
def get_timeline(timeline_id: uuid.UUID, db: Session = Depends(get_db)):
    return _read(db, timeline_service._get_timeline(db, timeline_id))


@router.patch("/timelines/{timeline_id}", response_model=TimelineRead)
def update_timeline(
    timeline_id: uuid.UUID, body: TimelineUpdate, db: Session = Depends(get_db)
):
    timeline = timeline_service.update_timeline(
        db,
        timeline_id,
        name=body.name,
        clip_order=[c.model_dump() for c in body.clip_order] if body.clip_order is not None else None,
        subtitle_mode=body.subtitle_mode,
        subtitle_track_ids=body.subtitle_track_ids,
    )
    db.commit()
    return _read(db, timeline)


@router.post("/timelines/{timeline_id}/build-from-shots", response_model=TimelineRead)
def build_from_shots(timeline_id: uuid.UUID, db: Session = Depends(get_db)):
    timeline = timeline_service.build_from_shots(db, timeline_id)
    db.commit()
    return _read(db, timeline)


@router.post(
    "/timelines/{timeline_id}/render",
    response_model=RenderAcceptedResponse,
    status_code=202,
)
def render(timeline_id: uuid.UUID, body: RenderRequest, db: Session = Depends(get_db)):
    result = render_service.request_render(
        db,
        timeline_id,
        export_preset_id=body.export_preset_id,
        ignore_warnings=body.ignore_warnings,
    )
    db.commit()
    return RenderAcceptedResponse.model_validate(result)


@router.get("/timelines/{timeline_id}/output")
def get_output(timeline_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    timeline = timeline_service._get_timeline(db, timeline_id)
    if not timeline.output_path:
        from app.core.errors import ErrorCode, StudioError

        raise StudioError(ErrorCode.NOT_FOUND, "Timeline has no render output yet.")
    abs_path = file_serve.resolve_file(timeline.output_path)
    return FileResponse(abs_path, media_type="video/mp4", filename="output.mp4")
