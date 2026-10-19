"""Seed loader for the model registry. `make seed` runs this; it is idempotent.

Usage: python -m app.models_lib.seed
"""
from __future__ import annotations

import json
from decimal import Decimal

from app.db.session import session_scope
from app.models.system import Model
from app.models_lib.registry import seed_dir


def load_seed_models() -> list[dict]:
    models: list[dict] = []
    for path in sorted(seed_dir().glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        models.extend(data.get("models", []))
    return models


def apply_seed() -> int:
    """Upsert every seed model by model_id. Returns the number of rows touched."""
    seeds = load_seed_models()
    touched = 0
    with session_scope() as db:
        for s in seeds:
            pricing = s.get("pricing") or {}
            row = db.get(Model, s["model_id"])
            values = {
                "modality": s["modality"],
                "provider_id": s["provider_id"],
                "status": s.get("status", "active"),
                "quality_rank": s.get("quality_rank", 0),
                "params_schema": s.get("params_schema", {}),
                "capabilities": s.get("capabilities", {}),
                "constraints": s.get("constraints", {}),
                "fallback_model_ids": s.get("fallback_model_ids", []),
                "pricing_unit": pricing.get("unit", "per_image"),
                "pricing_amount_usd": Decimal(str(pricing.get("amount_usd", "0"))),
            }
            if row is None:
                db.add(Model(model_id=s["model_id"], **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            touched += 1
    return touched


def main() -> None:
    n = apply_seed()
    print(f"seed: upserted {n} models (idempotent)")


if __name__ == "__main__":
    main()
