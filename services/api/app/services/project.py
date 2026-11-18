"""Project CRUD, archive, duplicate."""
from __future__ import annotations

import json
import shutil
import uuid

from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.files import paths
from app.models.audio import AudioGeneration
from app.models.generation import Generation, Video
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.story import Episode
from app.models.style import CategoryPack
from app.models.system import LLMCall
from app.services.budget_guard import _month_bounds_utc
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


def _count_by_project(query) -> dict[uuid.UUID, int]:
    return {pid: int(n) for pid, n in query.all()}


def _sum_by_project(rows) -> dict[uuid.UUID, Decimal]:
    return {pid: Decimal(str(total or 0)) for pid, total in rows}


def card_stats(db: Session, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Project-card summary for many projects at once (P0_SCREENS §1).

    A fixed number of grouped queries regardless of how many projects are
    listed (no per-project N+1):

    - thumbnail: approved frame of the first scene's first shot (thumbnail
      file if present, else the image), or None. Relative path, served by
      GET /files/{path}.
    - shot_count / approved_shot_count (approved_frame_generation_id set) /
      clip_count (videos with status 'done').
    - monthly_cost_actual_usd: the same current-UTC-month spend as
      budget_guard.spent_usd / the cost summary's total_actual_usd
      (succeeded generations + done audio, actual or else estimated cost,
      plus llm_calls).
    - monthly_cost_estimated_usd: this month's estimated_cost_usd sum over
      generations (batch parents excluded: their children carry the
      per-image estimates), audio_generations and llm_calls, skipping
      failed/canceled jobs.
    """
    ids = list(project_ids)
    if not ids:
        return {}

    episodes = _count_by_project(db.query(Episode.project_id, func.count(Episode.id))
                                 .filter(Episode.project_id.in_(ids))
                                 .group_by(Episode.project_id))
    scenes = _count_by_project(db.query(Scene.project_id, func.count(Scene.id))
                               .filter(Scene.project_id.in_(ids))
                               .group_by(Scene.project_id))
    shot_rows = (
        db.query(Scene.project_id, func.count(Shot.id), func.count(Shot.approved_frame_generation_id))
        .join(Shot, Shot.scene_id == Scene.id)
        .filter(Scene.project_id.in_(ids))
        .group_by(Scene.project_id)
        .all()
    )
    shots = {pid: (int(n), int(approved)) for pid, n, approved in shot_rows}
    clips = _count_by_project(db.query(Scene.project_id, func.count(Video.id))
                              .join(Shot, Shot.scene_id == Scene.id)
                              .join(Video, Video.shot_id == Shot.id)
                              .filter(Scene.project_id.in_(ids), Video.status == "done")
                              .group_by(Scene.project_id))

    # Representative thumbnail: first scene (order_index, created_at) ->
    # first shot (order_index, created_at) -> its approved, succeeded frame.
    first_scene: dict[uuid.UUID, uuid.UUID] = {}
    for pid, sid in (
        db.query(Scene.project_id, Scene.id)
        .filter(Scene.project_id.in_(ids))
        .order_by(Scene.order_index, Scene.created_at, Scene.id)
        .all()
    ):
        first_scene.setdefault(pid, sid)
    first_shot_gen: dict[uuid.UUID, uuid.UUID | None] = {}
    if first_scene:
        for sid, gen_id in (
            db.query(Shot.scene_id, Shot.approved_frame_generation_id)
            .filter(Shot.scene_id.in_(list(first_scene.values())))
            .order_by(Shot.order_index, Shot.created_at, Shot.id)
            .all()
        ):
            first_shot_gen.setdefault(sid, gen_id)
    gen_ids = [g for g in first_shot_gen.values() if g is not None]
    frame_path: dict[uuid.UUID, str] = {}
    if gen_ids:
        for gid, status, out, thumb in (
            db.query(Generation.id, Generation.status, Generation.output_path,
                     Generation.output_thumbnail_path)
            .filter(Generation.id.in_(gen_ids))
            .all()
        ):
            if status == "succeeded" and out:
                frame_path[gid] = thumb or out

    # Current-month cost (same period and actual-cost rule as budget_guard).
    start, end = _month_bounds_utc()
    actual: dict[uuid.UUID, Decimal] = {}
    estimated: dict[uuid.UUID, Decimal] = {}

    def _add(target: dict[uuid.UUID, Decimal], rows) -> None:
        for pid, total in _sum_by_project(rows).items():
            target[pid] = target.get(pid, Decimal(0)) + total

    def _gen_spend(model, done_status: str):
        q = (
            db.query(model.project_id,
                     func.sum(func.coalesce(model.actual_cost_usd, model.estimated_cost_usd, 0)))
            .filter(model.project_id.in_(ids), model.status == done_status,
                    model.reserved_at >= start, model.reserved_at <= end)
        )
        if model is Generation:
            # Batch parents duplicate their children's cost (same rule as
            # budget_guard.spent_usd since B5).
            q = q.filter(Generation.is_batch.is_(False))
        return q.group_by(model.project_id).all()

    _add(actual, _gen_spend(Generation, "succeeded"))
    _add(actual, _gen_spend(AudioGeneration, "done"))
    from sqlalchemy import or_

    def _llm_sum(status_filter):
        return (
            db.query(LLMCall.project_id, func.sum(func.coalesce(LLMCall.estimated_cost_usd, 0)))
            .filter(LLMCall.project_id.in_(ids), status_filter,
                    LLMCall.created_at >= start, LLMCall.created_at <= end)
            .group_by(LLMCall.project_id)
            .all()
        )

    # LLM status rules (budget_guard, plan D1): completed calls (legacy NULL
    # or 'succeeded') are actual spend; 'failed' calls never count.
    _add(actual, _llm_sum(or_(LLMCall.status.is_(None), LLMCall.status == "succeeded")))
    _add(estimated, _llm_sum(or_(LLMCall.status.is_(None), LLMCall.status != "failed")))
    _add(estimated, (
        db.query(Generation.project_id, func.sum(func.coalesce(Generation.estimated_cost_usd, 0)))
        .filter(Generation.project_id.in_(ids), Generation.is_batch.is_(False),
                Generation.status.notin_(("failed", "canceled")),
                Generation.reserved_at >= start, Generation.reserved_at <= end)
        .group_by(Generation.project_id)
        .all()
    ))
    _add(estimated, (
        db.query(AudioGeneration.project_id,
                 func.sum(func.coalesce(AudioGeneration.estimated_cost_usd, 0)))
        .filter(AudioGeneration.project_id.in_(ids),
                AudioGeneration.status.notin_(("failed", "canceled")),
                AudioGeneration.reserved_at >= start, AudioGeneration.reserved_at <= end)
        .group_by(AudioGeneration.project_id)
        .all()
    ))

    out: dict[uuid.UUID, dict[str, Any]] = {}
    for pid in ids:
        shot_n, approved_n = shots.get(pid, (0, 0))
        gen_id = first_shot_gen.get(first_scene[pid]) if pid in first_scene else None
        out[pid] = {
            "episode_count": episodes.get(pid, 0),
            "scene_count": scenes.get(pid, 0),
            "shot_count": shot_n,
            "approved_shot_count": approved_n,
            "clip_count": clips.get(pid, 0),
            "thumbnail": frame_path.get(gen_id) if gen_id is not None else None,
            "monthly_cost_estimated_usd": float(estimated.get(pid, Decimal(0))),
            "monthly_cost_actual_usd": float(actual.get(pid, Decimal(0))),
        }
    return out


def _enrich(
    project: Project,
    db: Session,
    stats: dict[str, Any] | None = None,
    pack_names: dict[str, str] | None = None,
) -> dict:
    """Project read model: row fields + card summary (design §8.1, P0_SCREENS §1).

    `pack_names` (code -> name) lets a list build every card without a
    per-project CategoryPack SELECT; single reads look the pack up directly."""
    pack_name = None
    if project.category_pack_code:
        if pack_names is not None:
            pack_name = pack_names.get(project.category_pack_code)
        else:
            pack = db.get(CategoryPack, project.category_pack_code)
            pack_name = pack.name if pack else None
    if stats is None:
        stats = card_stats(db, [project.id])[project.id]
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
        **stats,
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
    projects = q.order_by(Project.created_at.desc()).all()
    if not projects:
        return []
    stats = card_stats(db, [p.id for p in projects])
    # One query for every pack name used by the list (review B6-M2: a bare
    # preload kept no strong reference, so db.get re-selected per project).
    codes = {p.category_pack_code for p in projects if p.category_pack_code}
    pack_names = (
        dict(db.query(CategoryPack.code, CategoryPack.name).filter(CategoryPack.code.in_(codes)).all())
        if codes
        else {}
    )
    return [_enrich(p, db, stats[p.id], pack_names) for p in projects]


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
