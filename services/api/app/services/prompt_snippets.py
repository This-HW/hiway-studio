"""Prompt snippet library CRUD (P1-8, design report §4.20).

Snippets are free: no jobs, no budget checks. `project_id IS NULL` means a
global snippet that is usable in every project.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models.system import PromptSnippet

Scope = Literal["global", "project"]

_MAX_TAGS = 20
_TAG_MAX_LEN = 64


def _normalize_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    seen: list[str] = []
    for tag in tags:
        t = tag.strip()
        if not t or t in seen:
            continue
        if len(t) > _TAG_MAX_LEN:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"tag is longer than {_TAG_MAX_LEN} characters: {t!r}",
            )
        seen.append(t)
        if len(seen) > _MAX_TAGS:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"at most {_MAX_TAGS} tags per snippet",
            )
    return seen


def _project_exists(db: Session, project_id: uuid.UUID) -> bool:
    from app.models.project import Project

    return db.get(Project, project_id) is not None


def get_snippet(db: Session, snippet_id: uuid.UUID) -> PromptSnippet:
    row = db.get(PromptSnippet, snippet_id)
    if row is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Prompt snippet not found.")
    return row


def list_snippets(
    db: Session,
    *,
    category: str | None = None,
    tag: str | None = None,
    scope: Scope | None = None,
    project_id: uuid.UUID | None = None,
) -> list[PromptSnippet]:
    """Filter snippets.

    scope="global"  -> only global snippets (project_id IS NULL).
    scope="project" -> only the given project's snippets (project_id required).
    scope omitted   -> all, optionally narrowed by project_id when given.
    """
    q = db.query(PromptSnippet)
    if scope == "global":
        q = q.filter(PromptSnippet.project_id.is_(None))
    elif scope == "project":
        if project_id is None:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "project_id is required when scope=project.",
            )
        q = q.filter(PromptSnippet.project_id == project_id)
    elif project_id is not None:
        q = q.filter(PromptSnippet.project_id == project_id)
    if category:
        q = q.filter(PromptSnippet.category == category)
    rows = q.order_by(PromptSnippet.created_at.desc()).all()
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])]
    return rows


def create_snippet(db: Session, fields: dict[str, Any]) -> PromptSnippet:
    project_id = fields.get("project_id")
    if project_id is not None and not _project_exists(db, project_id):
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    row = PromptSnippet(
        title=fields["title"].strip(),
        body=fields["body"],
        negative=fields.get("negative"),
        category=(fields.get("category") or None),
        tags=_normalize_tags(fields.get("tags")),
        project_id=project_id,
    )
    if not row.title:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "title must not be blank.")
    db.add(row)
    db.flush()
    return row


def update_snippet(db: Session, snippet_id: uuid.UUID, fields: dict[str, Any]) -> PromptSnippet:
    row = get_snippet(db, snippet_id)
    if "title" in fields and fields["title"] is not None:
        title = fields["title"].strip()
        if not title:
            raise StudioError(ErrorCode.VALIDATION_ERROR, "title must not be blank.")
        row.title = title
    if "body" in fields and fields["body"] is not None:
        row.body = fields["body"]
    if "negative" in fields:
        row.negative = fields["negative"]
    if "category" in fields:
        row.category = fields["category"] or None
    if "tags" in fields and fields["tags"] is not None:
        row.tags = _normalize_tags(fields["tags"])
    db.flush()
    return row


def delete_snippet(db: Session, snippet_id: uuid.UUID) -> None:
    db.delete(get_snippet(db, snippet_id))
    db.flush()


# ADR-0011 §3: negative prompt library — 4 curated sets, seeded globally.
NEGATIVE_LIBRARY: list[dict[str, str]] = [
    {
        "title": "Negative: Quality",
        "category": "negative_library",
        "body": "",
        "negative": "low quality, blurry, out of focus, noisy, grainy, overexposed, underexposed, jpeg artifacts",
    },
    {
        "title": "Negative: Anatomy",
        "category": "negative_library",
        "body": "",
        "negative": "distorted, deformed, extra limbs, missing limbs, extra fingers, fused fingers, bad anatomy, disfigured",
    },
    {
        "title": "Negative: Text",
        "category": "negative_library",
        "body": "",
        "negative": "watermark, text artifacts, garbled text, logo, signature, caption, subtitle",
    },
    {
        "title": "Negative: Composition",
        "category": "negative_library",
        "body": "",
        "negative": "bad framing, cropped head, cropped limbs, tilted horizon, cluttered background, duplicate subjects",
    },
]


def seed_negative_library(db: Session) -> int:
    """Idempotently seed the 4 global negative sets (ADR-0011 §3)."""
    count = 0
    for spec in NEGATIVE_LIBRARY:
        exists = (
            db.query(PromptSnippet)
            .filter(
                PromptSnippet.project_id.is_(None),
                PromptSnippet.title == spec["title"],
            )
            .first()
        )
        if exists is None:
            db.add(
                PromptSnippet(
                    title=spec["title"],
                    body=spec["body"],
                    negative=spec["negative"],
                    category=spec["category"],
                    project_id=None,
                )
            )
            count += 1
    db.flush()
    return count


def global_negative_text(db: Session) -> str:
    """Combined negative text of the seeded library (falls back to default)."""
    rows = (
        db.query(PromptSnippet)
        .filter(
            PromptSnippet.project_id.is_(None),
            PromptSnippet.category == "negative_library",
        )
        .order_by(PromptSnippet.title)
        .all()
    )
    parts = [r.negative for r in rows if r.negative]
    if parts:
        return ", ".join(parts)
    from app.core import defaults

    return defaults.NEGATIVE_PROMPT_DEFAULT
