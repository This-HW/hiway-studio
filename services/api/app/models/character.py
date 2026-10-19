"""Characters and their reference images (design report §4.7).

project_id NULL = global library character, reusable across projects without
any "import" step (shot references query scope=all).

LoRA / embedding tables (character_loras, character_embeddings) arrive with
P1-4; they are intentionally absent here.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy import Uuid
from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # NULL = global library (cross-project reuse).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Contract enum character_role: protagonist|antagonist|supporting|extra.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="supporting")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    appearance_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Contract enum consistency_method.
    consistency_method: Mapped[str] = mapped_column(String(16), nullable=False, default="hybrid")
    seed: Mapped[int | None] = mapped_column(nullable=True)
    default_strength: Mapped[float] = mapped_column(nullable=False, default=0.8)
    voice_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_sample_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    style_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Portable JSON list (design says TEXT[]; JSON keeps SQLite tests working).
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_archived: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CharacterReference(Base):
    __tablename__ = "character_references"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Relative path only (projects/... or library/...).
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    # Contract enum reference_role: master|turnaround_*|expression_*|outfit_*|pose_*|detail_*.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_primary: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Contract enum reference_source: generated|uploaded|sheet_derived.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Exactly one primary reference per character (design §4.7).
        Index(
            "uq_character_primary",
            "character_id",
            unique=True,
            postgresql_where=text("is_primary = true"),
            sqlite_where=text("is_primary = 1"),
        ),
    )


class CharacterWardrobe(Base):
    __tablename__ = "character_wardrobe"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_fragment: Mapped[str] = mapped_column(Text, nullable=False)
    reference_image_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("character_references.id"), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_character_wardrobe_default",
            "character_id",
            unique=True,
            postgresql_where=text("is_default = true"),
            sqlite_where=text("is_default = 1"),
        ),
    )
