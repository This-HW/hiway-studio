"""Character schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

CharacterRole = Literal["protagonist", "antagonist", "supporting", "extra"]
ConsistencyMethod = Literal[
    "reference_image", "lora", "embedding", "faceid", "pulid", "seed_locked", "hybrid"
]
ReferenceSource = Literal["generated", "uploaded", "sheet_derived"]

# Roles are validated against contracts/enums.json reference_role at runtime;
# Literal would freeze the list here, so schemas accept str and services validate.
APPEARANCE_FIELDS = ("appearance_prompt", "negative_prompt", "consistency_method", "default_strength")


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    appearance_prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    consistency_method: ConsistencyMethod = "hybrid"
    default_strength: float = Field(default=0.8, ge=0.0, le=1.0)
    style_notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    project_id: uuid.UUID | None = None  # None = global library character


class CharacterUpdate(BaseModel):
    name: str | None = None
    role: CharacterRole | None = None
    description: str | None = None
    appearance_prompt: str | None = None
    negative_prompt: str | None = None
    consistency_method: ConsistencyMethod | None = None
    default_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    style_notes: str | None = None
    tags: list[str] | None = None


class CharacterRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    role: str
    description: str | None
    appearance_prompt: str
    negative_prompt: str | None
    consistency_method: str
    seed: int | None
    default_strength: float
    style_notes: str | None
    tags: list[str]
    version: int
    is_archived: bool
    referencing_shots: int = 0
    # Relative path of the primary master thumbnail (via /files/), if generated.
    master_thumbnail: str | None = None
    # Cloned voice id (P1-5 voice clone), if any.
    voice_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MasterGenerateRequest(BaseModel):
    prompt: str | None = None
    model_id: str | None = None


class JobAcceptedResponse(BaseModel):
    job_id: uuid.UUID


class SheetGenerateRequest(BaseModel):
    views: list[str] | None = None
    expressions: list[str] | None = None
    outfits: list[str] | None = None
    poses: list[str] | None = None


class SheetGenerateResponse(BaseModel):
    job_ids: list[uuid.UUID]
    estimated_cost_usd: Decimal


class ReferenceCreate(BaseModel):
    # Relative path of an uploaded file (e.g. from POST /assets/upload).
    # The service copies it into the character's references directory.
    image_path: str = Field(min_length=1)
    role: str = Field(min_length=1)
    is_primary: bool = False
    source: ReferenceSource = "uploaded"


class ReferenceUpdate(BaseModel):
    is_primary: bool | None = None
    role: str | None = None


class ReferenceRead(BaseModel):
    id: uuid.UUID
    character_id: uuid.UUID
    image_path: str
    role: str
    is_primary: bool
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class WardrobeCreate(BaseModel):
    name: str = Field(min_length=1)
    prompt_fragment: str = Field(min_length=1)
    reference_image_id: uuid.UUID | None = None
    is_default: bool = False


class WardrobeRead(BaseModel):
    id: uuid.UUID
    character_id: uuid.UUID
    name: str
    prompt_fragment: str
    reference_image_id: uuid.UUID | None
    is_default: bool
    created_at: datetime

    model_config = {"from_attributes": True}
