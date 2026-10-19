"""Episode routes (P1-10, design report §8.1).

GET/POST /projects/{id}/episodes — list & create.
GET/PATCH/DELETE /episodes/{id} — read & update & delete.
POST /episodes/{id}/clone — template deep copy (D-23): new ids everywhere,
copied shots are draft, generations/videos/audio are never copied.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import episode as s
from app.services import episode as episode_service

router = APIRouter(tags=["episodes"])


@router.get("/projects/{project_id}/episodes", response_model=list[s.EpisodeRead])
def list_episodes(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return episode_service.list_episodes(db, project_id)


@router.post("/projects/{project_id}/episodes", response_model=s.EpisodeRead,
             status_code=201)
def create_episode(project_id: uuid.UUID, body: s.EpisodeCreate,
                   db: Session = Depends(get_db)):
    episode = episode_service.create_episode(
        db, project_id,
        order_index=body.order_index, title=body.title,
        is_template=body.is_template,
    )
    db.commit()
    return episode


@router.get("/episodes/{episode_id}", response_model=s.EpisodeRead)
def get_episode(episode_id: uuid.UUID, db: Session = Depends(get_db)):
    return episode_service.get_episode(db, episode_id)


@router.patch("/episodes/{episode_id}", response_model=s.EpisodeRead)
def update_episode(episode_id: uuid.UUID, body: s.EpisodeUpdate,
                   db: Session = Depends(get_db)):
    episode = episode_service.update_episode(
        db, episode_id,
        order_index=body.order_index, title=body.title,
        is_template=body.is_template, status=body.status,
    )
    db.commit()
    return episode


@router.delete("/episodes/{episode_id}", status_code=204)
def delete_episode(episode_id: uuid.UUID, db: Session = Depends(get_db)):
    episode_service.delete_episode(db, episode_id)
    db.commit()
    return None


@router.post("/episodes/{episode_id}/clone", response_model=s.EpisodeCloneResponse,
             status_code=201)
def clone_episode(episode_id: uuid.UUID, db: Session = Depends(get_db)):
    clone = episode_service.clone_episode(db, episode_id)
    db.commit()
    return s.EpisodeCloneResponse(episode_id=clone.id)
