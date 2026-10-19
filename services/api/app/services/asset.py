"""Asset CRUD (design §4.10)."""
from __future__ import annotations

import shutil
import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.files import paths
from app.models.asset import Asset
from app.models.production import ShotReference
from app.models.project import Project

_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
# Phase 4: BGM/SFX uploads reuse the asset pipeline; kind=audio allows audio.
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}


def _asset_dir(project_id: uuid.UUID | None) -> str:
    if project_id is None:
        return "library/assets"
    return f"projects/{project_id}/assets"


def upload_asset(
    db: Session,
    data: bytes,
    filename: str,
    kind: str,
    name: str,
    project_id: uuid.UUID | None,
    prompt_fragment: str | None,
    tags: list[str],
) -> tuple[uuid.UUID, str]:
    check_enum("asset_kind", kind)
    if project_id is not None and db.get(Project, project_id) is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    if len(data) > defaults.UPLOAD_MAX_BYTES:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"File too large (max {defaults.UPLOAD_MAX_BYTES // (1024 * 1024)}MB).",
        )
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = _AUDIO_SUFFIXES if kind == "audio" else _ALLOWED_SUFFIXES
    if suffix not in allowed:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported {'audio' if kind == 'audio' else 'image'} type: {suffix or '(none)'}.",
        )
    asset_id = uuid.uuid4()
    rel = f"{_asset_dir(project_id)}/{asset_id}{suffix}"
    dst = paths.ensure_parent(paths.projects_root() / rel)
    dst.write_bytes(data)
    asset = Asset(
        id=asset_id,
        project_id=project_id,
        kind=kind,
        name=name,
        image_path=paths.assert_relative(rel),
        prompt_fragment=prompt_fragment,
        tags=tags,
    )
    db.add(asset)
    db.flush()
    return asset_id, asset.image_path


def list_assets(
    db: Session, project_id: uuid.UUID, scope: str = "all", kind: str | None = None
) -> list[Asset]:
    if scope == "project":
        q = db.query(Asset).filter(Asset.project_id == project_id)
    elif scope == "global":
        q = db.query(Asset).filter(Asset.project_id.is_(None))
    elif scope == "all":
        q = db.query(Asset).filter(
            (Asset.project_id == project_id) | (Asset.project_id.is_(None))
        )
    else:
        raise StudioError(ErrorCode.VALIDATION_ERROR, f"Unknown scope: {scope}.")
    if kind is not None:
        check_enum("asset_kind", kind)
        q = q.filter(Asset.kind == kind)
    return q.order_by(Asset.created_at).all()


def delete_asset(db: Session, asset_id: uuid.UUID) -> None:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Asset not found.")
    in_use = db.query(ShotReference).filter(ShotReference.asset_id == asset_id).count()
    if in_use:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Asset is referenced by {in_use} shot(s).",
        )
    db.delete(asset)
    db.flush()
    target = paths.resolve(asset.image_path)
    if target.exists():
        target.unlink(missing_ok=True)
