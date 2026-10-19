"""World bible entries (design report Phase 6, P2, ADR-0010).

A project's narrative bible: characters, factions, locations, world rules,
timeline events, and items. Free-form reference material that writers and
prompt assembly can consult; no generation pipeline reads it yet.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Contract enum bible_category (packages/contracts/enums.json).
BIBLE_CATEGORIES: tuple[str, ...] = (
    "character",
    "faction",
    "location",
    "rule",
    "event",
    "item",
)


class WorldBibleEntry(Base):
    __tablename__ = "world_bible_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Contract enum bible_category.
    category: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )
