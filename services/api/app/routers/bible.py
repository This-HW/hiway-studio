"""World bible routes (Phase 6, P2, ADR-0010). Thin: validate -> service -> respond."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.bible import BibleEntryCreate, BibleEntryRead, BibleEntryUpdate
from app.services import bible as bible_service

router = APIRouter(tags=["world-bible"])


@router.get("/projects/{project_id}/bible", response_model=list[BibleEntryRead])
def list_bible(
    project_id: uuid.UUID,
    category: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return bible_service.list_entries(db, project_id, category=category, tag=tag, q=q)


@router.post("/projects/{project_id}/bible", response_model=BibleEntryRead, status_code=201)
def create_bible_entry(
    project_id: uuid.UUID, body: BibleEntryCreate, db: Session = Depends(get_db)
):
    entry = bible_service.create_entry(
        db,
        project_id,
        category=body.category,
        title=body.title,
        body=body.body,
        tags=body.tags,
    )
    db.commit()
    return entry


@router.get("/bible/{entry_id}", response_model=BibleEntryRead)
def get_bible_entry(entry_id: uuid.UUID, db: Session = Depends(get_db)):
    return bible_service.get_entry(db, entry_id)


@router.patch("/bible/{entry_id}", response_model=BibleEntryRead)
def update_bible_entry(
    entry_id: uuid.UUID, body: BibleEntryUpdate, db: Session = Depends(get_db)
):
    entry = bible_service.update_entry(db, entry_id, body.model_dump(exclude_unset=True))
    db.commit()
    return entry


@router.delete("/bible/{entry_id}", status_code=204)
def delete_bible_entry(entry_id: uuid.UUID, db: Session = Depends(get_db)):
    bible_service.delete_entry(db, entry_id)
    db.commit()
    return None
