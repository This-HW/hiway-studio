"""Projects (design report §4.1).

A project owns a directory tree at projects/{uuid}/ (see app.files.paths).
Deleting a project deletes its directory too — there is no trash bin.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Contract enum aspect_ratio (contracts/enums.json). Stored as String with a
    # CHECK constraint in the migration (portable across Postgres/SQLite).
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False, default="16:9")
    default_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_models: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    style_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("style_kits.id"), nullable=True
    )
    category_pack_code: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("category_packs.code"), nullable=True
    )
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_archived: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"Project({self.id}, {self.title!r})"
