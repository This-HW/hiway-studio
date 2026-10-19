"""Post queue worker (q.post, concurrency 2).

Owns render jobs: run_render(timeline_id) — single argument. The DB row is
the source of truth (ARCHITECTURE §8); the heavy lifting lives in
services/render.py so the task stays a thin wrapper (tasks -> services OK).

Retry policy: render failures are deterministic (bad input / ffmpeg errors),
so they fail fast without burning retries. A render can always be requested
again after fixing the timeline.
"""
from __future__ import annotations

import logging

from celery import Task

from app.core import defaults
from app.core.celery_app import celery_app, register_job_dispatcher
from app.core.errors import StudioError
from app.services import render as render_service

log = logging.getLogger("ai_studio.tasks.post")


@celery_app.task(name="q.post.render_timeline", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def render_timeline(self: Task, timeline_id: str) -> dict:
    """Render one timeline end to end. Returns a summary dict."""
    try:
        summary = render_service.render_timeline(timeline_id)
        log.info("render succeeded timeline_id=%s", timeline_id)
        return summary
    except StudioError:
        # render_timeline already marked the timeline failed and published
        # render.completed{status: failed}; just re-raise for Celery bookkeeping.
        log.warning("render failed timeline_id=%s", timeline_id)
        raise
    except Exception:
        # Unexpected (non-StudioError) failures must not leave the timeline
        # stuck in "rendering": mark it failed here so the UI unlocks.
        log.exception("render crashed timeline_id=%s", timeline_id)
        render_service.fail_timeline_unexpected(timeline_id)
        raise


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.post.render_timeline", render_timeline.delay)


@celery_app.task(name="q.post.run_qc", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_qc(self: Task, qc_report_id: str) -> dict:
    """Run the 13-check QC gate for one report. Single task arg: the row is
    the source of truth (created by request_qc with overall='pending')."""
    from app.services import qc_orchestrator

    try:
        summary = qc_orchestrator.run_qc(qc_report_id)
        log.info("qc succeeded qc_report_id=%s", qc_report_id)
        return summary
    except Exception:
        log.exception("qc crashed qc_report_id=%s", qc_report_id)
        raise


register_job_dispatcher("q.post.run_qc", run_qc.delay)
