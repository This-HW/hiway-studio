"""Project CRUD, archive, duplicate."""
from __future__ import annotations

import json
import shutil
import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.files import paths
from app.models.production import Scene
from app.models.project import Project
from app.models.story import Episode
from app.models.style import CategoryPack
from app.services.episode_clone import duplicate_project as _duplicate_project


def create_project(
    db: Session,
    title: str,
    aspect_ratio: str,
    category_pack_code: str | None,
    default_models: dict | None,
) -> Project:
    check_enum("aspect_ratio", aspect_ratio)
    if category_pack_code is not None and db.get(CategoryPack, category_pack_code) is None:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Unknown category pack: {category_pack_code}.",
        )
    project = Project(
        id=uuid.uuid4(),
        title=title,
        aspect_ratio=aspect_ratio,
        category_pack_code=category_pack_code,
        default_models=default_models or {},
    )
    db.add(project)
    db.flush()
    project_dir = paths.project_dir(str(project.id))
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {"id": str(project.id), "title": title, "aspect_ratio": aspect_ratio},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return project


def _enrich(project: Project, db: Session) -> dict:
    """Project detail summary (design §8.1: 스토리·에피소드 수 등 요약 포함)."""
    pack_name = None
    if project.category_pack_code:
        pack = db.get(CategoryPack, project.category_pack_code)
        pack_name = pack.name if pack else None
    return {
        "id": project.id,
        "title": project.title,
        "aspect_ratio": project.aspect_ratio,
        "default_style": project.default_style,
        "default_models": project.default_models,
        "style_kit_id": project.style_kit_id,
        "category_pack_code": project.category_pack_code,
        "category_pack_name": pack_name,
        "monthly_budget_usd": project.monthly_budget_usd,
        "is_archived": project.is_archived,
        "episode_count": db.query(Episode).filter(Episode.project_id == project.id).count(),
        "scene_count": db.query(Scene).filter(Scene.project_id == project.id).count(),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def get_project(db: Session, project_id: uuid.UUID) -> dict:
    return _enrich(_get_project(db, project_id), db)


def _get_project(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def list_projects(db: Session, is_archived: bool = False) -> list[dict]:
    q = db.query(Project).filter(Project.is_archived.is_(is_archived))
    return [_enrich(p, db) for p in q.order_by(Project.created_at.desc()).all()]


def update_project(db: Session, project_id: uuid.UUID, fields: dict) -> dict:
    project = _get_project(db, project_id)
    if "aspect_ratio" in fields and fields["aspect_ratio"] is not None:
        check_enum("aspect_ratio", fields["aspect_ratio"])
    for key, value in fields.items():
        if value is not None and hasattr(project, key):
            setattr(project, key, value)
    db.flush()
    return _enrich(project, db)


def delete_project(db: Session, project_id: uuid.UUID, confirm: bool = False) -> None:
    if not confirm:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Project deletion needs ?confirm=true.",
        )
    project = _get_project(db, project_id)
    db.delete(project)
    db.flush()
    project_dir = paths.project_dir(str(project_id))
    if project_dir.exists():
        shutil.rmtree(project_dir)
    archive_zip = project_dir.with_suffix(".zip")
    if archive_zip.exists():
        archive_zip.unlink()


def _archive_zip_path(project_id: uuid.UUID):
    return paths.projects_root() / "projects" / f"{project_id}.zip"


def archive_project(db: Session, project_id: uuid.UUID) -> dict:
    """Archive: zip the project directory (§8.1) and set is_archived.

    Synchronous in Phase 1 (personal-scale projects); moves to a q.post job
    if archives ever grow slow.
    """
    project = _get_project(db, project_id)
    project_dir = paths.project_dir(str(project_id))
    if project_dir.exists():
        zip_path = _archive_zip_path(project_id)
        if zip_path.exists():
            zip_path.unlink()
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=project_dir)
    project.is_archived = True
    db.flush()
    return _enrich(project, db)


def unarchive_project(db: Session, project_id: uuid.UUID) -> dict:
    project = _get_project(db, project_id)
    project.is_archived = False
    db.flush()
    return _enrich(project, db)


def duplicate_project(db: Session, project_id: uuid.UUID) -> dict:
    clone = _duplicate_project(db, project_id)
    return _enrich(clone, db)
