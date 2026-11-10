"""Model capability declarations: which tasks a model serves and which
provider-input contract keys it consumes (ADR-0014).

Seed JSON (``capabilities``)::

    "tasks":  ["text_to_image", "inpaint"],      # job types this model serves
    "inputs": ["negative_prompt", "seed",         # contract keys it consumes
               "init_image", "mask", "reference_images"]

Rules (pure functions, no DB, no network):

* ``tasks`` declared -> the model serves exactly those tasks (an explicit
  empty list serves NOTHING). Undeclared (key absent / null) ->
  legacy behavior for generation tasks (serves every non-editing task) but
  NEVER an editing task: inpaint / outpaint / upscale / image_to_image need an
  explicit declaration, otherwise the request would silently run a text-only
  workflow and ignore the source image.
* ``inputs`` declared -> exactly those. Undeclared -> inferred from the
  adapter config: the stub consumes everything (it ignores inputs by design),
  an http_polling seed consumes the contract keys its templates reference,
  a cli seed consumes ``negative_prompt`` only.
* Editing inputs (``init_image``, ``mask``) the model cannot take are a hard
  reject (4xx, before any job row / budget reservation / dispatch).
  Reference conditioning a model cannot take degrades: the job runs
  text-only and the snapshot records an explicit warning (ADR-0014 §2).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.errors import ErrorCode, StudioError

EDITING_TASKS = ("image_to_image", "inpaint", "outpaint", "upscale")

# Job types that are plain text-to-image generations under another name.
TASK_ALIASES = {"thumbnail": "text_to_image"}

# Contract keys (see models_lib/contract.py).
INPUT_NEGATIVE = "negative_prompt"
INPUT_SEED = "seed"
INPUT_INIT_IMAGE = "init_image"
INPUT_MASK = "mask"
INPUT_REFERENCES = "reference_images"
ALL_INPUTS = (
    INPUT_NEGATIVE,
    INPUT_SEED,
    INPUT_INIT_IMAGE,
    INPUT_MASK,
    INPUT_REFERENCES,
)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Template placeholder -> contract input it proves the seed consumes.
_PLACEHOLDER_INPUT = {
    "negative_prompt": INPUT_NEGATIVE,
    "seed": INPUT_SEED,
    "init_image": INPUT_INIT_IMAGE,
    "init_image_data_uri": INPUT_INIT_IMAGE,
    "mask": INPUT_MASK,
    "mask_data_uri": INPUT_MASK,
    "reference_image": INPUT_REFERENCES,
    "reference_image_data_uri": INPUT_REFERENCES,
}


def canonical_task(task: str) -> str:
    return TASK_ALIASES.get(task, task)


def declared_tasks(model: dict[str, Any]) -> list[str] | None:
    """Declared task list; None only when the seed declares nothing (legacy).
    An explicit ``[]`` is a declaration: the model supports no task."""
    tasks = (model.get("capabilities") or {}).get("tasks")
    return None if tasks is None else list(tasks)


def supports_task(model: dict[str, Any], task: str) -> bool:
    task = canonical_task(task)
    declared = declared_tasks(model)
    if declared is not None:
        return task in declared
    return task not in EDITING_TASKS


def _placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_PLACEHOLDER_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _placeholders(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= _placeholders(v)
    return found


def http_placeholders(http: dict[str, Any]) -> set[str]:
    """Placeholders the http seed consumes: submit body + upload file sources."""
    names = _placeholders((http.get("submit") or {}).get("body_template") or {})
    uploads = http.get("upload") or []
    if isinstance(uploads, dict):
        uploads = [uploads]
    for up in uploads:
        names |= _placeholders(up.get("file_source") or "")
    return names


def accepted_inputs(model: dict[str, Any]) -> set[str]:
    caps = model.get("capabilities") or {}
    declared = caps.get("inputs")
    if declared is not None:
        return set(declared)
    provider = model.get("provider_id")
    if provider == "stub":
        return set(ALL_INPUTS)
    if provider == "cli":
        return {INPUT_NEGATIVE}
    http = caps.get("http")
    if isinstance(http, dict):
        return {
            _PLACEHOLDER_INPUT[name]
            for name in http_placeholders(http)
            if name in _PLACEHOLDER_INPUT
        }
    return set()


def check_request(
    model: dict[str, Any],
    task: str,
    *,
    has_init_image: bool = False,
    has_mask: bool = False,
    has_references: bool = False,
) -> list[dict[str, Any]]:
    """Enqueue-time capability pre-check.

    Raises VALIDATION_ERROR (400) for an unsupported task or editing input;
    returns the degrade warnings (reference conditioning the model cannot
    take) that the caller records in the generation snapshot.
    """
    model_id = model.get("model_id")
    if not supports_task(model, task):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Model {model_id} does not support task: {task}. "
            "Choose a model whose capabilities.tasks declares it.",
            details={
                "reason": "task_unsupported",
                "task": task,
                "model_id": model_id,
                "supported_tasks": declared_tasks(model) or [],
            },
        )
    accepted = accepted_inputs(model)
    missing = []
    if has_init_image and INPUT_INIT_IMAGE not in accepted:
        missing.append(INPUT_INIT_IMAGE)
    if has_mask and INPUT_MASK not in accepted:
        missing.append(INPUT_MASK)
    if missing:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Model {model_id} cannot take the input(s) {', '.join(missing)} "
            f"required by task {task}.",
            details={
                "reason": "input_unsupported",
                "task": task,
                "model_id": model_id,
                "inputs": missing,
            },
        )
    warnings: list[dict[str, Any]] = []
    if has_references and INPUT_REFERENCES not in accepted:
        warnings.append(
            {
                "code": "reference_unsupported",
                "message": (
                    f"Model {model_id} cannot take reference images; the job "
                    "runs text-only and identity/continuity references are "
                    "NOT applied."
                ),
            }
        )
    return warnings


def sampler_settings(model: dict[str, Any]) -> dict[str, Any]:
    """Sampler parameters fixed by an http workflow template (ComfyUI
    KSampler nodes): steps/cfg/sampler/scheduler/denoise, for the snapshot."""
    http = (model.get("capabilities") or {}).get("http") or {}
    body = (http.get("submit") or {}).get("body_template") or {}
    graph = body.get("prompt") if isinstance(body.get("prompt"), dict) else {}
    for node in graph.values():
        if isinstance(node, dict) and str(node.get("class_type", "")).startswith(
            "KSampler"
        ):
            inputs = node.get("inputs") or {}
            return {
                key: inputs[key]
                for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise")
                if key in inputs
            }
    return {}


def config_hash(model: dict[str, Any]) -> str:
    """Stable hash of the model's capabilities (workflow/template version)."""
    raw = json.dumps(model.get("capabilities") or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
