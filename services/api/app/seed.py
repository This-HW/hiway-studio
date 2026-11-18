"""Combined seed entry: model registry + built-in category packs.

Usage: python -m app.seed   (also via `make seed`; idempotent)

This is a top-level module so it may import both models_lib and services
without tripping the dependency-direction lint (neither may import the other).
"""
from __future__ import annotations

from app.core.contracts import seeds_dir
from app.db.session import session_scope
from app.models_lib.seed import apply_seed as apply_model_seed
from app.services.camera import ensure_builtin_camera_presets
from app.services.exports import ensure_builtin_presets
from app.services.prompt_snippets import seed_negative_library
from app.services.style_kit import seed_category_packs


def main() -> None:
    n = apply_model_seed()
    print(f"seed: upserted {n} models (idempotent)")
    seed_dir = seeds_dir() / "category_packs"
    with session_scope() as db:
        m = seed_category_packs(db, seed_dir)
        p = ensure_builtin_presets(db)
        c = ensure_builtin_camera_presets(db)
        neg = seed_negative_library(db)
    print(f"seed: upserted {m} category packs (idempotent)")
    print(f"seed: upserted {p} built-in export presets (idempotent)")
    print(f"seed: upserted {c} built-in camera presets (idempotent)")
    print(f"seed: upserted {neg} negative library sets (idempotent)")


if __name__ == "__main__":
    main()
