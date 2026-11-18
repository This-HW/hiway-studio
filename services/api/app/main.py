"""AI Studio API. Phase-1 surface: projects, stories, characters, assets, style. Phase 3: video, timelines, export presets."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core import defaults
from app.core.config import get_settings
from app.core.middleware import register_middleware
from app.routers import (
    assets,
    audio,
    bible,
    camera_presets,
    category_packs,
    characters,
    costs,
    episodes,
    events,
    exports,
    files,
    generations,
    health,
    models,
    multishots,
    previz,
    projects,
    qc,
    scenes,
    snippets,
    stories,
    style_kits,
    subtitles,
    thumbnails,
    timelines,
    variations,
    videos,
)


log = logging.getLogger("ai_studio.main")


async def _reconcile_loop() -> None:
    """Periodic job reconcile (ADR-0013, plan D4/H1).

    Runs in a thread so DB / broker calls never block the event loop; any
    error (DB down, broker unreachable) is logged and retried next interval.
    """
    from app.services import job_recovery

    while True:
        try:
            await asyncio.to_thread(job_recovery.reconcile_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive any failure
            log.exception("job reconcile pass failed")
        await asyncio.sleep(defaults.RECONCILE_INTERVAL_SEC)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Background task only: start-up never waits on the DB or the broker.
    task = asyncio.create_task(_reconcile_loop()) if get_settings().reconcile_active else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title="AI Studio API", version="0.3.0", lifespan=lifespan)
    register_middleware(app)
    # The web UI runs on a different origin (port 3000); without this the
    # browser blocks every API call (CORS). Origins are configurable via
    # CORS_ORIGINS env (layer 2), defaulting to the local web app (layer 1).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(files.router)  # GET /files/... — not under /api/v1
    for router in (
        projects.router,
        stories.router,
        characters.router,
        assets.router,
        style_kits.router,
        category_packs.router,
        models.router,
        scenes.router,
        generations.router,
        videos.router,
        timelines.router,
        exports.router,
        events.router,
        subtitles.router,
        audio.router,
        qc.router,
        costs.router,
        snippets.router,  # P1-8
        camera_presets.router,  # P1-2
        variations.router,  # P1-3
        multishots.router,  # P1-1
        thumbnails.router,  # P1-9
        episodes.router,  # P1-10
        bible.router,  # P2 (ADR-0010)
        previz.router,  # P2 (ADR-0010)
    ):
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
