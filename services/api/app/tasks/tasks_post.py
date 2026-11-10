"""Post queue worker (q.post, concurrency 2).

Owns render jobs: run_render(timeline_id) — single argument. The DB row is
the source of truth (ARCHITECTURE §8); the heavy lifting lives in
services/render.py so the task stays a thin wrapper (tasks -> services OK).

Retry policy: render failures are deterministic (bad input / ffmpeg errors /
postflight), so they fail fast without burning retries. A render can always
be requested again after fixing the timeline.

Render execution ownership (ADR-0013, plan D8): request_render claimed the
timeline with an exec_token; services/render.render_timeline marks the start
(a duplicate delivery is a no-op), commits `failed` itself on any error
(token-guarded) and publishes only through a token-guarded UPDATE.
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
        # render_timeline already COMMITTED the timeline as failed (token-
        # guarded) and published render.completed{failed}; re-raise for Celery.
        log.warning("render failed timeline_id=%s", timeline_id)
        raise
    except Exception:
        # render_timeline commits `failed` for crashes inside its execution
        # too. This net only covers a crash before the execution existed, and
        # only for legacy rows without an exec_token (reconcile handles the
        # rest by lease), so it can never overwrite a newer render.
        log.exception("render crashed timeline_id=%s", timeline_id)
        render_service.fail_timeline_unexpected(timeline_id)
        raise


# Register the eager-capable dispatcher so services can enqueue without
# importing this module (dependency DAG: services -> tasks is forbidden).
register_job_dispatcher("q.post.render_timeline", render_timeline.delay)


@celery_app.task(name="q.post.run_qc", bind=True, max_retries=defaults.RETRY_MAX_ATTEMPTS)
def run_qc(self: Task, qc_report_id: str) -> dict:
    """Run the 15-check QC gate for one report. Single task arg: the row is
    the source of truth (created by request_qc with overall='pending').

    On a crash outside the per-check isolation, qc_orchestrator.run_qc has
    already committed the report as overall='fail' (+ error_message) and
    published qc.completed before re-raising; here we only log for Celery
    bookkeeping (no retry — QC failures are deterministic)."""
    from app.services import qc_orchestrator

    try:
        summary = qc_orchestrator.run_qc(qc_report_id)
        log.info("qc succeeded qc_report_id=%s", qc_report_id)
        return summary
    except Exception:
        log.exception("qc crashed qc_report_id=%s (report marked fail)", qc_report_id)
        raise


register_job_dispatcher("q.post.run_qc", run_qc.delay)
