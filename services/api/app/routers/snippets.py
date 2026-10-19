"""Prompt snippet library routes (P1-8, design §8.1).

Snippet CRUD is free (no jobs, no cost). Thin: validate -> service -> respond.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.snippet import (
    PromptSnippetCreate,
    PromptSnippetRead,
    PromptSnippetUpdate,
)
from app.services import prompt_snippets as snippet_service

router = APIRouter(tags=["prompt-snippets"])


@router.get("/prompt-snippets", response_model=list[PromptSnippetRead])
def list_snippets(
    category: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    scope: Literal["project", "global"] | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[PromptSnippetRead]:
    rows = snippet_service.list_snippets(
        db, category=category, tag=tag, scope=scope, project_id=project_id
    )
    return [PromptSnippetRead.model_validate(r) for r in rows]


@router.post("/prompt-snippets", response_model=PromptSnippetRead, status_code=201)
def create_snippet(
    body: PromptSnippetCreate, db: Session = Depends(get_db)
) -> PromptSnippetRead:
    row = snippet_service.create_snippet(db, body.model_dump())
    return PromptSnippetRead.model_validate(row)


@router.get("/prompt-snippets/{snippet_id}", response_model=PromptSnippetRead)
def get_snippet(
    snippet_id: uuid.UUID, db: Session = Depends(get_db)
) -> PromptSnippetRead:
    return PromptSnippetRead.model_validate(snippet_service.get_snippet(db, snippet_id))


@router.patch("/prompt-snippets/{snippet_id}", response_model=PromptSnippetRead)
def update_snippet(
    snippet_id: uuid.UUID, body: PromptSnippetUpdate, db: Session = Depends(get_db)
) -> PromptSnippetRead:
    row = snippet_service.update_snippet(
        db, snippet_id, body.model_dump(exclude_unset=True)
    )
    return PromptSnippetRead.model_validate(row)


@router.delete("/prompt-snippets/{snippet_id}", status_code=204)
def delete_snippet(snippet_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    snippet_service.delete_snippet(db, snippet_id)
