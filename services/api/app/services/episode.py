"""Episode CRUD (P1-10, design report §4.3, §8.1).

Thin wrappers around the Episode model. The deep-copy logic lives in
services/episode_clone.py (D-23); this module reuses it unchanged —
copied shots reset to status=draft and generations/videos/audio are
never copied.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.models.story import Episode


def _project_or_404(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def _get_episode(db: Session, episode_id: uuid.UUID) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Episode not found.")
    return episode


def get_episode(db: Session, episode_id: uuid.UUID) -> Episode:
    """Read one episode; 404 NOT_FOUND when unknown."""
    return _get_episode(db, episode_id)


def list_episodes(db: Session, project_id: uuid.UUID) -> list[Episode]:
    _project_or_404(db, project_id)
    return (
        db.query(Episode)
        .filter(Episode.project_id == project_id)
        .order_by(Episode.order_index)
        .all()
    )


def create_episode(
    db: Session,
    project_id: uuid.UUID,
    order_index: int | None,
    title: str | None,
    is_template: bool,
) -> Episode:
    _project_or_404(db, project_id)
    if order_index is None:
        last = (
            db.query(Episode)
            .filter(Episode.project_id == project_id)
            .order_by(Episode.order_index.desc())
            .first()
        )
        order_index = (last.order_index + 1) if last else 0
    episode = Episode(
        id=uuid.uuid4(),
        project_id=project_id,
        order_index=order_index,
        title=title,
        is_template=is_template,
        status="draft",
    )
    db.add(episode)
    db.flush()
    return episode


def update_episode(
    db: Session,
    episode_id: uuid.UUID,
    order_index: int | None,
    title: str | None,
    is_template: bool | None,
    status: str | None,
) -> Episode:
    episode = _get_episode(db, episode_id)
    if order_index is not None:
        episode.order_index = order_index
    if title is not None:
        episode.title = title
    if is_template is not None:
        episode.is_template = is_template
    if status is not None:
        check_enum("episode_status", status)
        episode.status = status
    db.flush()
    return episode


def delete_episode(db: Session, episode_id: uuid.UUID) -> None:
    episode = _get_episode(db, episode_id)
    # Scenes detach (episode_id -> NULL) per the FK contract (§4.3,
    # ON DELETE SET NULL); the episode's content survives as unattached scenes.
    db.delete(episode)
    db.flush()


def clone_episode(db: Session, episode_id: uuid.UUID) -> Episode:
    """Deep-copy one episode (D-23) via services/episode_clone.py.

    New ids everywhere; copied shots are draft and regenerate from draft —
    generations, videos and audio are never copied.
    """
    from app.services.episode_clone import clone_episode as _clone

    clone = _clone(db, episode_id)
    db.flush()
    return clone
