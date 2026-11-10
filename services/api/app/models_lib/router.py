"""Model selection rules (design report §3.3).

Priority: explicit model -> project default -> router rule.
The chosen model and the reason are recorded for reproducibility.
Phase 0 implements explicit + rule-based; project defaults arrive with the
projects table (Phase 1).
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.errors import ErrorCode, StudioError
from app.models_lib import capabilities, registry


def route(
    task: str,
    modality: str,
    *,
    explicit_model_id: Optional[str] = None,
    project_default_model_id: Optional[str] = None,
    quality_tier: Optional[str] = None,
    db=None,
) -> dict[str, Any]:
    if explicit_model_id:
        model = registry.get_model(explicit_model_id, db)
        if model.get("status") == "disabled":
            raise StudioError(ErrorCode.MODEL_UNAVAILABLE, f"Model is disabled: {explicit_model_id}.")
        if not capabilities.supports_task(model, task):
            # A client choice, not an outage: 4xx, never retried (ADR-0014).
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Model {explicit_model_id} does not support task: {task}.",
                details={
                    "reason": "task_unsupported",
                    "task": task,
                    "supported_tasks": capabilities.declared_tasks(model) or [],
                },
            )
        return {"model_id": model["model_id"], "adapter": model["provider_id"], "reason": "explicit"}

    if project_default_model_id:
        model = registry.get_model(project_default_model_id, db)
        # A project default that cannot do this task (e.g. a t2i default for
        # an inpaint request) falls through to the router rule (ADR-0014).
        if capabilities.supports_task(model, task):
            return {
                "model_id": model["model_id"],
                "adapter": model["provider_id"],
                "reason": "project_default",
            }

    candidates = registry.list_by_modality(modality, db)
    if not candidates:
        raise StudioError(ErrorCode.MODEL_UNAVAILABLE, f"No active model for modality: {modality}.")

    # Task filter: models that declare capabilities.tasks only serve those
    # tasks (e.g. stub.i2v-* vs stub.t2v-*). Models without the declaration
    # serve every task except editing ones (inpaint/outpaint/upscale/i2i
    # need an explicit declaration, ADR-0014).
    task_aware = [m for m in candidates if capabilities.supports_task(m, task)]
    if task_aware:
        candidates = task_aware
    else:
        raise StudioError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"No active model supports task: {task}.",
        )

    if quality_tier == "final":
        # Final quality: highest quality_rank.
        chosen = max(candidates, key=lambda m: m.get("quality_rank", 0))
        reason = "router_rule:final_quality"
    else:
        # Draft default: cheapest. Phase-0 pricing lives in the seed; real
        # model_pricing comparison arrives with cost tracking (Phase 4).
        def _price(m: dict[str, Any]) -> float:
            pricing = m.get("pricing") or {}
            try:
                return float(pricing.get("amount_usd", 0))
            except (TypeError, ValueError):
                return 0.0

        chosen = min(candidates, key=_price)
        reason = "router_rule:draft_cheap"

    return {"model_id": chosen["model_id"], "adapter": chosen["provider_id"], "reason": reason}
