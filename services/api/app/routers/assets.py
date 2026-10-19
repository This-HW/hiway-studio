"""Asset endpoints (design §8.1: 에셋·업로드)."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Form, Query, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.asset import AssetRead, AssetUploadResponse
from app.services import asset as asset_service

router = APIRouter(tags=["assets"])


@router.post("/assets/upload", response_model=AssetUploadResponse, status_code=201)
async def upload_asset(
    file: UploadFile,
    kind: str = Form(default="background"),
    project_id: uuid.UUID | None = Form(default=None),
    db: Session = Depends(get_db),
) -> AssetUploadResponse:
    data = await file.read()
    asset_id, path = asset_service.upload_asset(
        db,
        data=data,
        filename=file.filename or "upload.png",
        kind=kind,
        name=file.filename or "untitled",
        project_id=project_id,
        prompt_fragment=None,
        tags=[],
    )
    return AssetUploadResponse(asset_id=asset_id, path=path)


@router.get("/projects/{project_id}/assets", response_model=list[AssetRead])
def list_assets(
    project_id: uuid.UUID,
    kind: str | None = Query(default=None),
    scope: Literal["project", "global", "all"] = Query(default="all"),
    db: Session = Depends(get_db),
) -> list[AssetRead]:
    return [
        AssetRead.model_validate(a)
        for a in asset_service.list_assets(db, project_id, scope, kind)
    ]


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    asset_service.delete_asset(db, asset_id)
