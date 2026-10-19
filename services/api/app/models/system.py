"""Model registry tables (design report §4.19, §4.21).

Phase-1 additions: llm_calls (text-LLM call history, G-12).
model_providers / model_pricing / prompt_snippets arrive in Phase 4 with
cost tracking. Pricing stays flattened on models until then.
export_presets arrived in Phase 3 (P0-18, ADR-0004).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Model(Base):
    __tablename__ = "models"

    # Business key, e.g. "stub.t2i-fast" (naming rule: provider_alias.model_name).
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    modality: Mapped[str] = mapped_column(String(32), nullable=False)  # image|video|text|tts|...
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)  # adapter key
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    quality_rank: Mapped[int] = mapped_column(default=0)  # higher = better (router)
    params_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    constraints: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fallback_model_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    pricing_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="per_image")
    pricing_amount_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Model({self.model_id}, {self.modality}, {self.status})"


class LLMCall(Base):
    """Text-LLM call history (design report §4.19, G-12).

    Text LLMs are managed through the model registry (modality='text', D-16).
    The system prompt template and its version are stored with every call so
    story generation stays reproducible.
    """

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Contract enum llm_purpose: synopsis|beats|broll_suggest|translation|dialogue_polish|other.
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    model_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("models.model_id"), nullable=False
    )
    system_prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PromptSnippet(Base):
    """Prompt snippet library (P1-8, design report §4.20).

    `project_id IS NULL` = global snippet (usable in every project).
    Snippet CRUD is free (no jobs, no cost).
    """

    __tablename__ = "prompt_snippets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # prompt fragment
    negative: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExportPreset(Base):
    """Platform output preset (P0-18). `project_id IS NULL` = built-in preset.

    Built-in presets are immutable from the API (only custom, project-scoped
    presets can be created/edited/deleted).
    """

    __tablename__ = "export_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # youtube | youtube_shorts | instagram_reels | tiktok | custom.
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Contract enum aspect_ratio.
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "1920x1080"
    fps: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    codec: Mapped[str] = mapped_column(Text, nullable=False, default="h264")
    audio_codec: Mapped[str] = mapped_column(Text, nullable=False, default="aac")
    burn_subtitles: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
