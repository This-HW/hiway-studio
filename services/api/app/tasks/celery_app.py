"""Celery application entry point for the worker.

The shared instance lives in app.core.celery_app (importable from services
without a cycle). This module registers the task modules so the worker
discovers them; `celery -A app.tasks.celery_app worker ...` still works.
"""
from __future__ import annotations

from app.core.celery_app import celery_app

# Register task modules so the worker discovers them.
celery_app.conf.include = [
    "app.tasks.tasks_image",
    "app.tasks.tasks_video",
    "app.tasks.tasks_audio",
    "app.tasks.tasks_post",
    # P1 tasks (q.p1.* names) ride the q.post queue — 4 queues stay fixed
    # (ARCHITECTURE §8). Without this import the worker never registers
    # the q.p1.* handlers and dispatches fail as unregistered tasks.
    "app.tasks.tasks_p1",
]

__all__ = ["celery_app"]
