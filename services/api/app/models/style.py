"""Style kits and category packs (design report §4.9, §4.10).

Style kits: project_id NULL = global template. Locked kits are immutable;
edit them by duplicating (D-31). Inheritance: shot -> scene -> project.
Category packs: 3 builtin packs seeded from infra/seeds/category_packs/.
"장르시트" is the user's alias for this table.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StyleKit(Base):
    __tablename__ = "style_kits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # NULL = global template.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_locked: Mapped[bool] = mapped_column(nullable=False, default=False)
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    lighting: Mapped[str | None] = mapped_column(Text, nullable=True)
    texture: Mapped[str | None] = mapped_column(Text, nullable=True)
    composition: Mapped[str | None] = mapped_column(Text, nullable=True)
    lens: Mapped[str | None] = mapped_column(Text, nullable=True)
    motion: Mapped[str | None] = mapped_column(Text, nullable=True)
    editing_rhythm: Mapped[str | None] = mapped_column(Text, nullable=True)
    sound: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_source_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CategoryPack(Base):
    __tablename__ = "category_packs"

    # knowledge_info | drama_shortform | ai_influencer_daily (builtin) or user clones.
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_builtin: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
