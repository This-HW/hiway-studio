"""Story / beat endpoints (G-5)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.story import (
    BeatCreate,
    BeatRead,
    BeatUpdate,
    BeatsGenerateRequest,
    BeatsGenerateResponse,
    ScriptImportResponse,
    StoryRead,
    StoryUpdate,
    SynopsisGenerateRequest,
    SynopsisGenerateResponse,
)
from app.services import story as story_service

router = APIRouter(tags=["stories"])


@router.get("/projects/{project_id}/story", response_model=StoryRead)
def get_story(project_id: uuid.UUID, db: Session = Depends(get_db)) -> StoryRead:
    return StoryRead.model_validate(story_service.get_story(db, project_id))


@router.put("/projects/{project_id}/story", response_model=StoryRead)
def update_story(
    project_id: uuid.UUID, body: StoryUpdate, db: Session = Depends(get_db)
) -> StoryRead:
    story = story_service.update_story(db, project_id, body.logline, body.synopsis)
    return StoryRead.model_validate(story)


@router.post(
    "/projects/{project_id}/story/generate-synopsis",
    response_model=SynopsisGenerateResponse,
    status_code=201,
)
def generate_synopsis(
    project_id: uuid.UUID, body: SynopsisGenerateRequest, db: Session = Depends(get_db)
) -> SynopsisGenerateResponse:
    llm_call_id, synopsis = story_service.generate_synopsis(
        db, project_id, body.logline, body.model_id
    )
    return SynopsisGenerateResponse(llm_call_id=llm_call_id, synopsis=synopsis)


@router.post(
    "/projects/{project_id}/story/generate-beats",
    response_model=BeatsGenerateResponse,
    status_code=201,
)
def generate_beats(
    project_id: uuid.UUID, body: BeatsGenerateRequest, db: Session = Depends(get_db)
) -> BeatsGenerateResponse:
    llm_call_id, beats = story_service.generate_beats(
        db, project_id, body.act_count, body.model_id
    )
    return BeatsGenerateResponse(
        llm_call_id=llm_call_id,
        beats=[BeatRead.model_validate(b) for b in beats],
    )


@router.get("/stories/{story_id}/beats", response_model=list[BeatRead])
def list_beats(story_id: uuid.UUID, db: Session = Depends(get_db)) -> list[BeatRead]:
    return [BeatRead.model_validate(b) for b in story_service.list_beats(db, story_id)]


@router.post("/stories/{story_id}/beats", response_model=BeatRead, status_code=201)
def create_beat(
    story_id: uuid.UUID, body: BeatCreate, db: Session = Depends(get_db)
) -> BeatRead:
    beat = story_service.create_beat(
        db, story_id, body.order_index, body.act, body.title, body.summary
    )
    return BeatRead.model_validate(beat)


@router.patch("/beats/{beat_id}", response_model=BeatRead)
def update_beat(
    beat_id: uuid.UUID, body: BeatUpdate, db: Session = Depends(get_db)
) -> BeatRead:
    beat = story_service.update_beat(db, beat_id, **body.model_dump(exclude_unset=True))
    return BeatRead.model_validate(beat)


@router.delete("/beats/{beat_id}", status_code=204)
def delete_beat(beat_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    story_service.delete_beat(db, beat_id)


@router.post(
    "/projects/{project_id}/story/import",
    response_model=ScriptImportResponse,
    status_code=201,
)
async def import_script(
    project_id: uuid.UUID,
    file: UploadFile,
    script_format: str = Form(default="txt", alias="format"),
    db: Session = Depends(get_db),
) -> ScriptImportResponse:
    content = (await file.read()).decode("utf-8", errors="replace")
    scenes_created = story_service.import_script(db, project_id, content, script_format)
    return ScriptImportResponse(scenes_created=scenes_created)
