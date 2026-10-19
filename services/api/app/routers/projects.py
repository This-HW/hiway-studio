"""Project endpoints (design §8.1: 프로젝트)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.project import (
    ProjectArchiveResponse,
    ProjectCreate,
    ProjectDuplicateResponse,
    ProjectRead,
    ProjectUpdate,
)
from app.services import project as project_service

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectRead, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> ProjectRead:
    project = project_service.create_project(
        db,
        title=body.title,
        aspect_ratio=body.aspect_ratio,
        category_pack_code=body.category_pack_code,
        default_models=body.default_models,
    )
    return ProjectRead.model_validate(project_service.get_project(db, project.id))


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(
    is_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[ProjectRead]:
    return [ProjectRead.model_validate(p) for p in project_service.list_projects(db, is_archived)]


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: uuid.UUID, db: Session = Depends(get_db)) -> ProjectRead:
    return ProjectRead.model_validate(project_service.get_project(db, project_id))


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, db: Session = Depends(get_db)
) -> ProjectRead:
    project = project_service.update_project(
        db, project_id, body.model_dump(exclude_unset=True)
    )
    return ProjectRead.model_validate(project)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: uuid.UUID,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> None:
    project_service.delete_project(db, project_id, confirm=confirm)


@router.post("/projects/{project_id}/archive", response_model=ProjectArchiveResponse)
def archive_project(project_id: uuid.UUID, db: Session = Depends(get_db)) -> ProjectArchiveResponse:
    project = project_service.archive_project(db, project_id)
    return ProjectArchiveResponse(job_id=project["id"], status="archived")


@router.post("/projects/{project_id}/unarchive", response_model=ProjectRead)
def unarchive_project(project_id: uuid.UUID, db: Session = Depends(get_db)) -> ProjectRead:
    project = project_service.unarchive_project(db, project_id)
    return ProjectRead.model_validate(project)


@router.post("/projects/{project_id}/duplicate", response_model=ProjectDuplicateResponse, status_code=201)
def duplicate_project(project_id: uuid.UUID, db: Session = Depends(get_db)) -> ProjectDuplicateResponse:
    clone = project_service.duplicate_project(db, project_id)
    return ProjectDuplicateResponse(id=clone["id"], title=clone["title"])
