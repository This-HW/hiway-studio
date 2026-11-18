"""Provider input contract for still-image jobs (ADR-0014 §1).

One explicit, provider-neutral payload. The enqueue path resolves every
input once and stores it in the generation's snapshot
(``params["snapshot"]``, relative paths only); the image worker builds the
adapter payload from that snapshot; each adapter maps the contract keys to
its own protocol (ComfyUI uploads the files and fills ``{init_image}`` /
``{mask}`` / ``{reference_image}`` placeholders; other http seeds can take
``{<key>_data_uri}``; the cli adapter takes prompt + negative and rejects
editing inputs).

Payload keys (all adapters receive the same dict)::

    task             job type (text_to_image, inpaint, ...)
    prompt           final prompt (row.prompt_final)
    negative_prompt  final negative (row.negative_final), "" when none
    seed             int, always concrete (persisted at enqueue)
    width / height   int
    init_image       absolute path of the source image (editing / i2i), optional
    mask             absolute path of the mask (inpaint / outpaint), optional
    references       [{"path": abs, "role": str, "weight": float}, ...] (priority order)
    reference_image  absolute path of references[0] (single-reference templates)
    reference_weight weight of references[0]

Pure module: no DB, no files, no network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core import defaults
from app.models_lib import capabilities

CONTRACT_VERSION = 1
SNAPSHOT_KEY = "snapshot"

KEY_TASK = "task"
KEY_PROMPT = "prompt"
KEY_NEGATIVE = "negative_prompt"
KEY_SEED = "seed"
KEY_WIDTH = "width"
KEY_HEIGHT = "height"
KEY_INIT_IMAGE = "init_image"
KEY_MASK = "mask"
KEY_REFERENCES = "references"
KEY_REFERENCE_IMAGE = "reference_image"
KEY_REFERENCE_WEIGHT = "reference_weight"

# Payload keys that hold LOCAL file paths (adapters upload / encode them).
FILE_KEYS = (KEY_INIT_IMAGE, KEY_MASK, KEY_REFERENCE_IMAGE)


def reference_entry(
    path: str,
    role: str,
    *,
    kind: str = "",
    ref_id: str | None = None,
    weight: float | None = None,
) -> dict[str, Any]:
    """One resolved reference (relative path, as stored in the snapshot)."""
    return {
        "path": path,
        "role": role,
        "kind": kind or role,
        "ref_id": ref_id,
        "weight": defaults.REFERENCE_DEFAULT_WEIGHT
        if weight is None
        else float(weight),
    }


def build_snapshot(
    model: dict[str, Any],
    *,
    task: str,
    negative_prompt: str,
    seed: int,
    seed_source: str,
    width: int | None = None,
    height: int | None = None,
    references: list[dict[str, Any]] | None = None,
    init_image: str | None = None,
    mask: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    style_kit_id: str | None = None,
    style_kit_version: int | None = None,
) -> dict[str, Any]:
    """Resolved generation parameters at enqueue (ADR-0011 §14, ADR-0014).

    ``references`` keep their full list; when the model cannot take them
    (warning ``reference_unsupported``) ``references_applied`` is False and
    the worker sends none — the record still shows what was resolved.
    """
    refs = list(references or [])
    warn = list(warnings or [])
    applied = bool(refs) and not any(
        w.get("code") == "reference_unsupported" for w in warn
    )
    cfg_hash = capabilities.config_hash(model)
    return {
        "contract_version": CONTRACT_VERSION,
        "task": task,
        "model_id": model.get("model_id"),
        "model_version": model.get("version") or f"cfg-{cfg_hash[:12]}",
        "model_config_sha256": cfg_hash,
        "provider_id": model.get("provider_id"),
        "seed": int(seed),
        "seed_source": seed_source,
        "width": int(width or defaults.IMAGE_DEFAULT_WIDTH),
        "height": int(height or defaults.IMAGE_DEFAULT_HEIGHT),
        "sampler": capabilities.sampler_settings(model),
        "negative_prompt": negative_prompt or "",
        "references": refs,
        "references_applied": applied,
        "init_image": init_image,
        "mask": mask,
        "warnings": warn,
        "style_kit_id": style_kit_id,
        "style_kit_version": style_kit_version,
    }


def image_payload(
    snapshot: dict[str, Any],
    *,
    prompt: str,
    negative_prompt: str | None,
    seed: int,
    resolve: Callable[[str], str],
) -> dict[str, Any]:
    """Adapter payload from a snapshot. ``resolve`` maps a stored relative
    path to the absolute local path (the worker owns the projects root)."""
    payload: dict[str, Any] = {
        KEY_TASK: snapshot.get("task") or "text_to_image",
        KEY_PROMPT: prompt or "",
        KEY_NEGATIVE: negative_prompt or "",
        KEY_SEED: int(seed),
        KEY_WIDTH: int(snapshot.get("width") or defaults.IMAGE_DEFAULT_WIDTH),
        KEY_HEIGHT: int(snapshot.get("height") or defaults.IMAGE_DEFAULT_HEIGHT),
    }
    if snapshot.get("init_image"):
        payload[KEY_INIT_IMAGE] = resolve(snapshot["init_image"])
    if snapshot.get("mask"):
        payload[KEY_MASK] = resolve(snapshot["mask"])
    refs: list[dict[str, Any]] = []
    if snapshot.get("references_applied"):
        refs = [
            {
                "path": resolve(r["path"]),
                "role": r.get("role", ""),
                "weight": float(r.get("weight", defaults.REFERENCE_DEFAULT_WEIGHT)),
            }
            for r in snapshot.get("references") or []
            if r.get("path")
        ]
    payload[KEY_REFERENCES] = refs
    if refs:
        payload[KEY_REFERENCE_IMAGE] = refs[0]["path"]
        payload[KEY_REFERENCE_WEIGHT] = refs[0]["weight"]
    return payload
