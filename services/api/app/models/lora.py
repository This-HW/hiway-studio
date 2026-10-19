"""LoRA / embedding models per character (design report §4.7, P1-4).

A LoraModel is the trained weight file plus its training metadata.
Training runs as the q.p1.run_lora_train job (target row id = this row);
on success the weight file lands at
projects/{project_uuid}/characters/{character_uuid}/lora/{trigger_word}.safetensors
(design §11.1) and the character's consistency_method is switched to 'lora'
so later prompts reference it (§7.2 / ComfyUI loras/ convention).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Contract enum lora_status: training | ready | failed.
LORA_STATUSES: tuple[str, ...] = ("training", "ready", "failed")

# Design [가정]: LoRA training is only offered with at least 8 source images.
MIN_TRAINING_IMAGES = 8


class LoraModel(Base):
    __tablename__ = "lora_models"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    trigger_word: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    training_image_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CharacterReference ids used for training (the worker's source of truth,
    # ADR-0008: the DB row, not the task args, carries the job parameters).
    training_image_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Contract enum lora_status.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="training")
    # Relative path only, e.g.
    # "projects/<uuid>/characters/<uuid>/lora/<trigger>.safetensors".
    model_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Base model the LoRA was trained against (e.g. "flux-dev").
    base_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    # Cost-ledger row (job_type='lora_train') so budget aggregation keeps
    # working through the single generations-based path (design §9.4).
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generations.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
