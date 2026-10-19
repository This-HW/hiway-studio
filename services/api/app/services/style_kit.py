"""Style kits (design §4.17) and category packs (G-17, ADR-0013)."""
from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models.project import Project
from app.models.style import CategoryPack, StyleKit
from app.schemas.style import STYLE_KIT_FIELDS

STYLE_LOCKED_FIELDS = set(STYLE_KIT_FIELDS)


def _kit_or_404(db: Session, kit_id: uuid.UUID) -> StyleKit:
    kit = db.get(StyleKit, kit_id)
    if kit is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Style kit not found.")
    return kit


def list_style_kits(db: Session, project_id: uuid.UUID | None) -> list[StyleKit]:
    q = db.query(StyleKit)
    if project_id is None:
        q = q.filter(StyleKit.project_id.is_(None))
    else:
        q = q.filter(
            (StyleKit.project_id == project_id) | (StyleKit.project_id.is_(None))
        )
    return q.order_by(StyleKit.created_at).all()


def create_style_kit(
    db: Session, name: str, project_id: uuid.UUID | None, fields: dict
) -> StyleKit:
    if project_id is not None and db.get(Project, project_id) is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    kit = StyleKit(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        version=1,
        is_locked=False,
        **{k: fields.get(k) for k in STYLE_KIT_FIELDS},
    )
    db.add(kit)
    db.flush()
    return kit


def update_style_kit(db: Session, kit_id: uuid.UUID, fields: dict) -> StyleKit:
    kit = _kit_or_404(db, kit_id)
    if kit.is_locked:
        raise StudioError(
            ErrorCode.STYLE_KIT_LOCKED,
            "Style kit is locked. Duplicate it to edit.",
        )
    changed = False
    for key, value in fields.items():
        if key == "name" and value is not None:
            kit.name = value
            changed = True
        elif key in STYLE_LOCKED_FIELDS and value is not None:
            setattr(kit, key, value)
            changed = True
    if changed:
        kit.version += 1
    db.flush()
    return kit


def lock_style_kit(db: Session, kit_id: uuid.UUID) -> StyleKit:
    kit = _kit_or_404(db, kit_id)
    kit.is_locked = True
    db.flush()
    return kit


def duplicate_style_kit(db: Session, kit_id: uuid.UUID) -> StyleKit:
    src = _kit_or_404(db, kit_id)
    clone = StyleKit(
        id=uuid.uuid4(),
        project_id=src.project_id,
        name=f"{src.name} (copy)",
        version=1,
        is_locked=False,
        **{k: getattr(src, k) for k in STYLE_KIT_FIELDS},
    )
    db.add(clone)
    db.flush()
    return clone


def list_category_packs(db: Session) -> list[CategoryPack]:
    return db.query(CategoryPack).order_by(CategoryPack.code).all()


def seed_category_packs(db: Session, seed_dir) -> int:
    """Idempotent seed of built-in category packs from JSON files."""
    touched = 0
    for path in sorted(seed_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for pack in data.get("category_packs", []):
            row = db.get(CategoryPack, pack["code"])
            values = {
                "name": pack["name"],
                "description": pack.get("description"),
                "pipeline_config": pack.get("pipeline_config", {}),
                "is_builtin": True,
            }
            if row is None:
                db.add(CategoryPack(code=pack["code"], **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            touched += 1
    return touched
