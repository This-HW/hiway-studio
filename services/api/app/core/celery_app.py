"""Shared Celery application instance.

Lives in core (not tasks/) so both sides can import it without a dependency
cycle: services enqueue jobs via send_task, task modules register handlers.
core -> core only (config), so the dependency DAG stays acyclic.

Four fixed queues: q.image / q.video / q.audio / q.post. Never add queues;
adjust concurrency via settings instead (ARCHITECTURE §8).
"""
from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "ai_studio",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_routes={
        "q.image.*": {"queue": "q.image"},
        "q.video.*": {"queue": "q.video"},
        "q.audio.*": {"queue": "q.audio"},
        "q.post.*": {"queue": "q.post"},
        # P1 orchestration jobs are lightweight fan-out / completion tasks;
        # they ride the q.post queue (no new queues, ARCHITECTURE §8).
        "q.p1.*": {"queue": "q.post"},
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
)


# --- job dispatch ------------------------------------------------------------
# Services enqueue jobs without importing task modules (the dependency DAG
# forbids services -> tasks). Task modules register their .delay callable
# here at import; dispatch_job uses it when available (worker process, and
# tests that import the task module), otherwise falls back to send_task
# (API process -> Redis -> worker).
_JOB_DISPATCHERS: dict[str, object] = {}


def register_job_dispatcher(task_name: str, dispatch_fn: object) -> None:
    _JOB_DISPATCHERS[task_name] = dispatch_fn


def dispatch_job(task_name: str, *args: object) -> None:
    dispatch_fn = _JOB_DISPATCHERS.get(task_name)
    if dispatch_fn is not None:
        dispatch_fn(*args)  # type: ignore[operator]
    else:
        celery_app.send_task(task_name, args=list(args))
