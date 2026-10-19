"""Asset schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AssetKind = Literal["location", "prop", "background"]


class AssetRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    kind: str
    name: str
    image_path: str
    prompt_fragment: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AssetUploadResponse(BaseModel):
    asset_id: uuid.UUID
    path: str
