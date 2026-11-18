"""Enqueue-time provider input resolution (ADR-0014).

Every still-image job resolves its provider inputs ONCE, at enqueue, into the
generation snapshot (``params["snapshot"]``, see models_lib/contract.py):
model id/version (config hash), task, seed (+ where it came from), size,
sampler settings fixed by the workflow, negative prompt, the reference image
paths actually used with their roles/weights, init image and mask. The
capability pre-check runs here too, so an unsupported request is a 4xx
BEFORE any job row, budget reservation or dispatch exists.
"""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.files import paths
from app.models_lib import capabilities, contract, registry


def draw_seed() -> int:
    """A fresh random seed; callers persist it (reproducible runs)."""
    return random.randrange(defaults.IMAGE_SEED_MAX_EXCLUSIVE)


def _inside_root(rel: str) -> bool:
    root = paths.projects_root().resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return False
    return target.is_file()


def _usable_references(
    references: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep references whose file exists inside the projects root."""
    kept: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for ref in references or []:
        rel = ref.get("path")
        if not rel:
            continue
        if Path(str(rel)).is_absolute() or not _inside_root(str(rel)):
            warnings.append(
                {
                    "code": "reference_missing",
                    "message": f"Reference image not found under the projects root: {rel}.",
                    "path": str(rel),
                    "role": ref.get("role"),
                }
            )
            continue
        kept.append(
            contract.reference_entry(
                str(rel),
                str(ref.get("role") or "reference"),
                kind=str(ref.get("kind") or ""),
                ref_id=ref.get("ref_id"),
                weight=ref.get("weight"),
            )
        )
    return kept, warnings


def image_snapshot(
    db: Session,
    *,
    model_id: str,
    task: str,
    seed: int,
    seed_source: str,
    negative_prompt: str | None = "",
    references: list[dict[str, Any]] | None = None,
    init_image: str | None = None,
    mask: str | None = None,
    style_kit_id: Any = None,
    style_kit_version: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Capability pre-check + resolved snapshot. Raises VALIDATION_ERROR (4xx)
    for an unsupported task / editing input; reference conditioning the
    model cannot take is recorded as a warning (job runs text-only)."""
    model = registry.get_model(model_id, db)
    refs, warnings = _usable_references(references)
    warnings = (
        capabilities.check_request(
            model,
            task,
            has_init_image=bool(init_image),
            has_mask=bool(mask),
            has_references=bool(refs),
        )
        + warnings
    )
    return contract.build_snapshot(
        model,
        task=task,
        negative_prompt=negative_prompt or "",
        seed=seed,
        seed_source=seed_source,
        width=width,
        height=height,
        references=refs,
        init_image=init_image,
        mask=mask,
        warnings=warnings,
        style_kit_id=str(style_kit_id) if style_kit_id else None,
        style_kit_version=style_kit_version,
    )


def with_seed(snapshot: dict[str, Any], seed: int) -> dict[str, Any]:
    """Per-child copy of a batch snapshot with the child's own seed."""
    snap = copy.deepcopy(snapshot)
    snap["seed"] = int(seed)
    return snap
