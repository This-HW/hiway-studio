"""World bible CRUD (Phase 6, P2, ADR-0010).

Thin domain wrapper: category validation against the contract enum,
project scoping, and entry-scoped lookups. No jobs, no cost — free.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.models.project import Project
from app.models.world_bible import BIBLE_CATEGORIES, WorldBibleEntry


def _require_project(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, f"Project not found: {project_id}.")
    return project


def list_entries(
    db: Session,
    project_id: uuid.UUID,
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
) -> list[WorldBibleEntry]:
    _require_project(db, project_id)
    query = (
        db.query(WorldBibleEntry)
        .filter(WorldBibleEntry.project_id == project_id)
        .order_by(WorldBibleEntry.category, WorldBibleEntry.title)
    )
    if category is not None:
        check_enum("bible_category", category)
        query = query.filter(WorldBibleEntry.category == category)
    entries = query.all()
    if tag is not None:
        entries = [e for e in entries if tag in (e.tags or [])]
    if q is not None:
        needle = q.lower()
        entries = [
            e for e in entries
            if needle in e.title.lower() or needle in (e.body or "").lower()
        ]
    return entries


def create_entry(
    db: Session,
    project_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    tags: list[str],
) -> WorldBibleEntry:
    _require_project(db, project_id)
    check_enum("bible_category", category)
    if category not in BIBLE_CATEGORIES:  # belt-and-suspenders with the model const
        raise StudioError(ErrorCode.VALIDATION_ERROR, f"Invalid bible category: {category}.")
    entry = WorldBibleEntry(
        id=uuid.uuid4(),
        project_id=project_id,
        category=category,
        title=title.strip(),
        body=body or "",
        tags=list(tags or []),
    )
    db.add(entry)
    db.flush()
    return entry


def get_entry(db: Session, entry_id: uuid.UUID) -> WorldBibleEntry:
    entry = db.get(WorldBibleEntry, entry_id)
    if entry is None:
        raise StudioError(ErrorCode.NOT_FOUND, f"Bible entry not found: {entry_id}.")
    return entry


def update_entry(db: Session, entry_id: uuid.UUID, patch: dict) -> WorldBibleEntry:
    entry = get_entry(db, entry_id)
    if "category" in patch and patch["category"] is not None:
        check_enum("bible_category", patch["category"])
        entry.category = patch["category"]
    if "title" in patch and patch["title"] is not None:
        entry.title = patch["title"].strip()
    if "body" in patch and patch["body"] is not None:
        entry.body = patch["body"]
    if "tags" in patch and patch["tags"] is not None:
        entry.tags = list(patch["tags"])
    db.flush()
    return entry


def delete_entry(db: Session, entry_id: uuid.UUID) -> None:
    entry = get_entry(db, entry_id)
    db.delete(entry)
    db.flush()
