"""Model registry: DB-backed catalog with seed-JSON bootstrap (design report §3.2).

Lookup order: DB row first (operator edits), seed JSON fallback (fresh installs).
Text LLMs are included with modality='text' (D-16) — the registry is not
image/video-only.
"""
from __future__ import annotations

import contextvars
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from app.core.contracts import seeds_dir
from app.core.errors import ErrorCode, StudioError
from app.models_lib.adapters.http_polling import HttpPollingAdapter
from app.models_lib.adapters.cli import CliAdapter
from app.models_lib.adapters.stub import StubAdapter
from app.models_lib.base import ProviderAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    StubAdapter.provider_id: StubAdapter(),
    HttpPollingAdapter.provider_id: HttpPollingAdapter(),
    CliAdapter.provider_id: CliAdapter(),
}

_seed_cache: dict[str, dict[str, Any]] | None = None

# The model entry a worker resolved (DB overlay) and is executing right now.
# Adapters look their config up by model_id without a DB session; inside
# ``pinned_model`` they get exactly the entry the worker hashed and recorded
# (B7-r2 M4) instead of the seed cache.
_pinned: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "registry_pinned_model", default=None
)


@contextmanager
def pinned_model(model: dict[str, Any]) -> Iterator[dict[str, Any]]:
    token = _pinned.set(model)
    try:
        yield model
    finally:
        _pinned.reset(token)


def seed_dir() -> Path:
    return seeds_dir() / "models"


def _load_seed_cache() -> dict[str, dict[str, Any]]:
    global _seed_cache
    if _seed_cache is None:
        cache: dict[str, dict[str, Any]] = {}
        for path in sorted(seed_dir().glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for model in data.get("models", []):
                cache[model["model_id"]] = model
        _seed_cache = cache
    return _seed_cache


def get_adapter(provider_id: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[provider_id]
    except KeyError:
        raise StudioError(ErrorCode.MODEL_UNAVAILABLE, f"Unknown provider adapter: {provider_id}.")


def get_model(model_id: str, db=None) -> dict[str, Any]:
    """Return the registry entry. db is an optional SQLAlchemy session for the
    DB-overlay lookup; without it the pinned entry (``pinned_model``) of
    that model_id, else the seed cache is used."""
    pinned = _pinned.get()
    if db is None and pinned is not None and pinned.get("model_id") == model_id:
        return pinned
    if db is not None:
        from app.models.system import Model  # noqa: PLC0415 (deferred: models_lib -> models is allowed)

        row = db.get(Model, model_id)
        if row is not None:
            return {
                "model_id": row.model_id,
                "modality": row.modality,
                "provider_id": row.provider_id,
                "status": row.status,
                "quality_rank": row.quality_rank,
                "params_schema": row.params_schema,
                "capabilities": row.capabilities,
                "constraints": row.constraints,
                "fallback_model_ids": row.fallback_model_ids,
            }
    cache = _load_seed_cache()
    try:
        return cache[model_id]
    except KeyError:
        raise StudioError(ErrorCode.NOT_FOUND, f"Model not found in registry: {model_id}.")


# Largest value Postgres accepts for statement_timeout / lock_timeout (ms).
_PG_TIMEOUT_MAX_MS = 2_147_483_647


@contextmanager
def _bounded_session(db, timeout: float | None) -> Iterator[None]:
    """Bound this session's DB work by ``timeout`` seconds where the stack
    allows it (B9a-r2 M4).

    Postgres: ``SET LOCAL statement_timeout`` + ``lock_timeout`` (they end
    with this transaction). SQLite: ``PRAGMA busy_timeout`` for the lock
    wait, restored afterwards (the pooled connection keeps its own value).
    A limit already in force is never loosened (B9a-r3 M2): the value set
    is min(existing, budget). Postgres 0 means "no limit", so the budget
    applies; SQLite busy_timeout 0 means "never wait for a lock", which is
    already stricter than any budget and is kept. NOT bounded: the wait for a pooled connection (engine ``pool_timeout``)
    and opening a new connection (no per-call connect timeout on the shared
    engine) — ADR-0014 B9a-r2.
    """
    if timeout is None:
        yield
        return
    ms = min(_PG_TIMEOUT_MAX_MS, max(1, math.ceil(timeout * 1000)))
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy import text  # noqa: PLC0415 (deferred: rarely used)

        # pg_settings.setting is in the base unit (ms) for both; 0 = no limit.
        current = dict(
            db.execute(
                text(
                    "SELECT name, setting FROM pg_settings "
                    "WHERE name IN ('statement_timeout', 'lock_timeout')"
                )
            ).all()
        )
        for name in ("statement_timeout", "lock_timeout"):
            existing = int(current.get(name) or 0)
            value = ms if existing <= 0 else min(existing, ms)
            db.execute(text(f"SET LOCAL {name} = {value}"))
        yield
        return
    if dialect == "sqlite":
        conn = db.connection()
        previous = int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0)
        # 0 (never wait) is kept: min() never loosens it.
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {min(previous, ms)}")
        try:
            yield
        finally:
            conn.exec_driver_sql(f"PRAGMA busy_timeout = {previous}")
        return
    yield


def resolve_model(model_id: str, *, timeout: float | None = None) -> dict[str, Any]:
    """The entry a worker executes for ``model_id``, for a caller without a
    DB session (B9 M3: restoring a persisted provider job handle).

    The pinned entry (``pinned_model``) when it is this model's; else the DB
    overlay read in its own session (``get_model(model_id, db)``: DB row
    first, the seed only when the model has no row). A failing DB read is
    MODEL_UNAVAILABLE, never a silent fallback to the seed.

    ``timeout`` (seconds left in the caller's budget) bounds the DB work
    where the stack allows it (``_bounded_session``; B9a-r2 M4); no time
    left -> MODEL_UNAVAILABLE without touching the DB.
    """
    pinned = _pinned.get()
    if pinned is not None and pinned.get("model_id") == model_id:
        return pinned
    if timeout is not None and timeout <= 0:
        raise StudioError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"Model {model_id}: no time left to read the registry entry from "
            "the database.",
            details={"reason": "deadline_exceeded"},
        )
    from app.db.session import session_scope  # noqa: PLC0415 (deferred: as seed.py)

    try:
        with session_scope() as db, _bounded_session(db, timeout):
            return get_model(model_id, db)
    except StudioError:
        raise
    except Exception as exc:  # noqa: BLE001 (DB unavailable: reported, never seed)
        raise StudioError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"Model {model_id}: the registry entry could not be read from the "
            f"database ({type(exc).__name__}); not falling back to the seed.",
        ) from exc


def list_models(db=None, *, active_only: bool = True) -> list[dict[str, Any]]:
    """All registry models (seed JSON overlaid with DB rows)."""
    models = list(_load_seed_cache().values())
    if db is not None:
        from app.models.system import Model  # noqa: PLC0415

        for row in db.query(Model).all():
            models = [m for m in models if m["model_id"] != row.model_id]
            models.append(
                {
                    "model_id": row.model_id,
                    "modality": row.modality,
                    "provider_id": row.provider_id,
                    "status": row.status,
                    "quality_rank": row.quality_rank,
                    "params_schema": row.params_schema,
                    "capabilities": row.capabilities,
                    "constraints": row.constraints,
                    "fallback_model_ids": row.fallback_model_ids,
                }
            )
    if active_only:
        models = [m for m in models if m.get("status") == "active"]
    return models


def list_by_modality(modality: str, db=None, *, active_only: bool = True) -> list[dict[str, Any]]:
    models = [m for m in _load_seed_cache().values() if m["modality"] == modality]
    if db is not None:
        from app.models.system import Model  # noqa: PLC0415

        for row in db.query(Model).filter(Model.modality == modality).all():
            models = [m for m in models if m["model_id"] != row.model_id]
            models.append(
                {
                    "model_id": row.model_id,
                    "modality": row.modality,
                    "provider_id": row.provider_id,
                    "status": row.status,
                    "quality_rank": row.quality_rank,
                    "params_schema": row.params_schema,
                    "capabilities": row.capabilities,
                    "constraints": row.constraints,
                    "fallback_model_ids": row.fallback_model_ids,
                }
            )
    if active_only:
        models = [m for m in models if m.get("status") == "active"]
    return models


def reset_cache() -> None:
    """Tests only."""
    global _seed_cache
    _seed_cache = None
