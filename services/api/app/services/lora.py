"""LoRA training (P1-4, design §4.7 character_loras, §8.1).

Flow:
  1. POST /characters/{id}/lora/train -> request_train(): validates the
     character and the training images (CharacterReference ids), enforces the
     8-image minimum (422 LORA_TOO_FEW_IMAGES unless confirm=true), checks the
     budget (per_call), creates the lora_models row (+ a generations ledger
     row with job_type='lora_train' when the character belongs to a project,
     so budget aggregation keeps working through the single generations path),
     and dispatches q.p1.run_lora_train (task arg = the lora row id, ADR-0008).
  2. complete_train() (worker): the stub adapter completes immediately —
     the weight file placeholder is written, the row goes ready, and the
     character's consistency_method is switched to 'lora' so later prompts
     reference the trigger word (design §7.2; ComfyUI reads loras/ by file name).

A real training backend plugs in at complete_train(): replace the stub file
write with the provider's training call, keeping the row transitions.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import dispatch_after_commit
from app.files import immutability, paths
from app.models.lora import MIN_TRAINING_IMAGES, LoraModel
from app.services import budget_guard, cost_estimator

# Per-call priced training model (seed; a real training backend replaces the
# pricing via the DB overlay). The stub completes immediately at $0.
TRAIN_MODEL_ID = "stub.lora-train"

JOB_TYPE = "lora_train"


def _get_character(db: Session, character_id: uuid.UUID):
    from app.models.character import Character

    character = db.get(Character, character_id)
    if character is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Character not found.")
    return character


def _character_dir(character) -> str:
    if character.project_id is None:
        return f"library/characters/{character.id}"
    return f"projects/{character.project_id}/characters/{character.id}"


def _publish(job_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    get_bridge().publish(CHANNEL, event, {"job_id": str(job_id), "job_type": JOB_TYPE, **data})


def request_train(
    db: Session,
    character_id: uuid.UUID,
    *,
    training_image_ids: list[uuid.UUID],
    rank: int | None,
    steps: int | None,
    trigger_word: str,
    confirm: bool = False,
) -> dict[str, Any]:
    from app.models.character import CharacterReference
    from app.models.generation import Generation

    character = _get_character(db, character_id)
    refs = []
    for rid in training_image_ids:
        ref = db.get(CharacterReference, rid)
        if ref is None or ref.character_id != character.id:
            raise StudioError(
                ErrorCode.NOT_FOUND,
                "Training image not found for this character.",
                details={"training_image_id": str(rid)},
            )
        refs.append(ref)
    image_count = len(refs)
    if image_count < MIN_TRAINING_IMAGES and not confirm:
        raise StudioError(
            ErrorCode.LORA_TOO_FEW_IMAGES,
            f"LoRA training needs at least {MIN_TRAINING_IMAGES} images "
            f"({image_count} given). Resubmit with confirm=true to proceed anyway.",
            details={"image_count": image_count, "minimum": MIN_TRAINING_IMAGES},
        )

    check_enum("job_type", JOB_TYPE)
    estimated = cost_estimator.estimate(db, TRAIN_MODEL_ID)
    if character.project_id is not None:
        budget_guard.check_budget(db, character.project_id, estimated_new=estimated)

    lora = LoraModel(
        character_id=character.id,
        trigger_word=trigger_word,
        rank=rank,
        steps=steps,
        training_image_count=image_count,
        training_image_ids=[str(r.id) for r in refs],
        status="training",
        estimated_cost_usd=estimated,
    )
    db.add(lora)
    db.flush()

    # Cost ledger so the per_call price lands in the budget summary (§9.4).
    # Global characters have no project to bill; their cost lives on the row.
    if character.project_id is not None:
        gen = Generation(
            project_id=character.project_id,
            shot_id=None,
            job_type=JOB_TYPE,
            model_id=TRAIN_MODEL_ID,
            estimated_cost_usd=estimated,
            params={"lora_model_id": str(lora.id)},
        )
        db.add(gen)
        db.flush()
        lora.generation_id = gen.id
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.p1.run_lora_train", str(lora.id))
    )
    return {
        "job_id": lora.id,
        "lora_model_id": lora.id,
        "estimated_cost_usd": estimated,
    }


def complete_train(db: Session, lora_model_id: str) -> dict[str, Any]:
    """Worker: finish training (stub completes immediately)."""
    from app.models.generation import Generation

    lora = db.get(LoraModel, uuid.UUID(lora_model_id))
    if lora is None:
        raise StudioError(ErrorCode.NOT_FOUND, "LoRA model not found.")
    if lora.status != "training":
        return {"lora_model_id": str(lora.id), "status": lora.status, "resumed": True}

    _publish(lora.id, "job.progress",
             {"status": "training", "progress_pct": 50.0, "stage": "training"})

    character = _get_character(db, lora.character_id)
    rel_path = f"{_character_dir(character)}/lora/{lora.trigger_word}.safetensors"
    abs_path = paths.ensure_parent(paths.projects_root() / rel_path)
    # Stub weight file: a real backend writes the trained .safetensors here.
    immutability.write_new(
        abs_path,
        f"# stub LoRA weights for trigger '{lora.trigger_word}' "
        f"(character {lora.character_id}); replace with a real training backend.\n".encode(),
    )
    lora.model_path = paths.assert_relative(rel_path)
    lora.status = "ready"
    lora.actual_cost_usd = lora.estimated_cost_usd

    # Link the character: later prompts use the LoRA + trigger word (§7.2).
    character.consistency_method = "lora"

    if lora.generation_id is not None:
        gen = db.get(Generation, lora.generation_id)
        if gen is not None:
            from datetime import datetime, timezone

            gen.status = "succeeded"
            gen.progress_pct = 100.0
            gen.actual_cost_usd = gen.estimated_cost_usd
            gen.finished_at = datetime.now(timezone.utc)
    db.flush()

    _publish(lora.id, "job.completed",
             {"status": "succeeded", "output_path": lora.model_path, "output_count": 1})
    return {"lora_model_id": str(lora.id), "status": "ready", "model_path": lora.model_path}


def fail_train(db: Session, lora_model_id: str, code: str, message: str) -> None:
    lora = db.get(LoraModel, uuid.UUID(lora_model_id))
    if lora is None or lora.status != "training":
        return
    lora.status = "failed"
    db.flush()
    _publish(lora.id, "job.failed",
             {"status": "failed", "error_code": code, "error_message": message,
              "retryable": False, "retry_count": 0, "max_retries": 0})


def get_lora(db: Session, lora_id: uuid.UUID) -> LoraModel:
    lora = db.get(LoraModel, lora_id)
    if lora is None:
        raise StudioError(ErrorCode.NOT_FOUND, "LoRA model not found.")
    return lora


def list_loras(db: Session, character_id: uuid.UUID) -> list[LoraModel]:
    _get_character(db, character_id)
    return (
        db.query(LoraModel)
        .filter(LoraModel.character_id == character_id)
        .order_by(LoraModel.created_at.desc())
        .all()
    )
