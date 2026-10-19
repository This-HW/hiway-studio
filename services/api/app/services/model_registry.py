"""Model registry read service (design §3.2, §8.1).

Read-only view over models_lib.registry for the model-picker dropdown,
plus operator edits (PATCH /models/{id}): status and fallback chain are
stored as a DB overlay row — the registry reads the DB row first and falls
back to the seed JSON (models_lib/registry.py).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.models.system import Model
from app.models_lib import registry


def list_models(db: Session, modality: str | None, status: str = "active") -> list[dict]:
    models = registry.list_models(db, active_only=(status == "active"))
    if modality is not None:
        models = [m for m in models if m.get("modality") == modality]
    if status != "active":
        models = [m for m in registry.list_models(db, active_only=False) if m.get("status") == status]
        if modality is not None:
            models = [m for m in models if m.get("modality") == modality]
    return models


def get_model(db: Session, model_id: str) -> dict:
    try:
        return registry.get_model(model_id, db)
    except StudioError as exc:
        raise StudioError(ErrorCode.NOT_FOUND, f"Model not found: {model_id}.") from exc


def update_model(
    db: Session,
    model_id: str,
    *,
    status: str | None = None,
    fallback_model_ids: list[str] | None = None,
) -> dict:
    """Operator edit: model enable/disable and fallback chain (design §8.1).

    Upserts a DB overlay row seeded from the registry entry; the registry
    prefers the DB row over the seed JSON afterwards.
    """
    if status is not None:
        check_enum("model_status", status)
    if fallback_model_ids is not None:
        if not isinstance(fallback_model_ids, list) or not all(
            isinstance(m, str) for m in fallback_model_ids
        ):
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "fallback_model_ids must be a list of model id strings.",
            )
        for fallback_id in fallback_model_ids:
            try:
                registry.get_model(fallback_id, db)
            except StudioError as exc:
                raise StudioError(
                    ErrorCode.NOT_FOUND,
                    f"Fallback model not found: {fallback_id}.",
                ) from exc

    row = db.get(Model, model_id)
    if row is None:
        try:
            seed = registry.get_model(model_id)
        except StudioError as exc:
            raise StudioError(ErrorCode.NOT_FOUND, f"Model not found: {model_id}.") from exc
        row = Model(
            model_id=seed["model_id"],
            modality=seed.get("modality", "image"),
            provider_id=seed.get("provider_id", ""),
            status=seed.get("status", "active"),
            quality_rank=seed.get("quality_rank", 0),
            params_schema=seed.get("params_schema", {}),
            capabilities=seed.get("capabilities", {}),
            constraints=seed.get("constraints", {}),
            fallback_model_ids=seed.get("fallback_model_ids", []),
            pricing_unit=seed.get("pricing_unit", "per_image"),
            pricing_amount_usd=seed.get("pricing_amount_usd", 0),
        )
        db.add(row)
    if status is not None:
        row.status = status
    if fallback_model_ids is not None:
        row.fallback_model_ids = list(fallback_model_ids)
    db.flush()
    return registry.get_model(model_id, db)
