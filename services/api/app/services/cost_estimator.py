"""Cost estimation (design report §9).

Phase-2 scope: estimation only. Enforcement (budget_guard, 403 blocking)
arrives in Phase 4.

Unit formulas (§9.1):
- per_image:   unit_price_usd × count
- per_second:  unit_price_usd × duration_sec
- per_call:    unit_price_usd (flat)
- per_1k_chars:  unit_price_usd × ceil(chars / 1000)
- per_1k_tokens: unit_price_usd × ceil(tokens / 1000)
- per_char:      unit_price_usd × chars (TTS seed models, design §9.1 variant)

Pricing source: the `models` table (DB overlay) first, seed-cache fallback.
upscale / inpaint / outpaint bill at the image model's per_image price
([assumption] #9.1 note).
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models_lib import registry


def get_pricing(db: Session | None, model_id: str) -> tuple[str, Decimal]:
    """Return (unit, amount_usd) for a model. Raises MODEL_UNAVAILABLE."""
    if db is not None:
        from app.models.system import Model  # deferred: services -> models is allowed

        row = db.get(Model, model_id)
        if row is not None:
            return str(row.pricing_unit), Decimal(str(row.pricing_amount_usd))
    seed = registry.get_model(model_id)  # seed cache; raises if unknown
    pricing = seed.get("pricing") or {}
    unit = str(pricing.get("unit", "per_image"))
    return unit, Decimal(str(pricing.get("amount_usd", "0")))


def estimate(
    db: Session | None,
    model_id: str,
    *,
    count: int = 1,
    duration_sec: float = 0.0,
    chars: int = 0,
    tokens: int = 0,
) -> Decimal:
    """Estimate cost in USD for one request, following the unit formula."""
    unit, amount = get_pricing(db, model_id)
    if unit == "per_image":
        return amount * count
    if unit == "per_second":
        return amount * Decimal(str(duration_sec))
    if unit == "per_call":
        return amount
    if unit == "per_1k_chars":
        return amount * math.ceil(chars / 1000)
    if unit == "per_char":
        return amount * chars
    if unit == "per_1k_tokens":
        return amount * math.ceil(tokens / 1000)
    raise StudioError(
        ErrorCode.VALIDATION_ERROR,
        f"Unknown pricing unit for model {model_id}: {unit}.",
    )


def estimate_text(db: Session | None, model_id: str, chars: int) -> Decimal:
    """Estimate TTS/LLM text cost from a character count (per_char,
    per_1k_chars)."""
    if chars < 0:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "chars must be >= 0.")
    return estimate(db, model_id, chars=chars)


def estimate_images(db: Session | None, model_id: str, count: int) -> Decimal:
    """Estimate image-generation cost (frame batches, retakes, inpaint,
    upscale, outpaint — all bill per_image per output image)."""
    if count < 1:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "count must be >= 1.")
    return estimate(db, model_id, count=count)


def pricing_breakdown(db: Session | None, model_id: str, count: int) -> dict[str, Any]:
    unit, amount = get_pricing(db, model_id)
    total = estimate(db, model_id, count=count)
    return {
        "model_id": model_id,
        "unit": unit,
        "unit_price_usd": str(amount),
        "count": count,
        "estimated_cost_usd": str(total),
    }
