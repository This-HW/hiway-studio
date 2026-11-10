"""Model registry: DB-backed catalog with seed-JSON bootstrap (design report §3.2).

Lookup order: DB row first (operator edits), seed JSON fallback (fresh installs).
Text LLMs are included with modality='text' (D-16) — the registry is not
image/video-only.
"""
from __future__ import annotations

import contextvars
import json
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
