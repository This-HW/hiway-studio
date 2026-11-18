"""Character master/sheet generation pipeline (D-18, D-19).

Enqueues Generation rows whose params carry the character linkage:
  params["_character_id"], params["_reference_role"], params["_source"].
The worker's post-processing (tasks.post_generation) reads those keys and
creates the CharacterReference row — see the CONTRACT comment below.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.core.celery_app import dispatch_job
from app.models.character import Character, CharacterReference
from app.models.generation import Generation
from app.models_lib import registry, router as model_router
from app.db.session import dispatch_after_commit
from app.core.contracts import check_enum, enum_values


def _enqueue_image_job(db: Session, generation_id: uuid.UUID) -> None:
    """Put the generation on q.image after the row commits. Task arg is the id only (§8)."""
    dispatch_after_commit(
        db, lambda: dispatch_job("q.image.run_generation", str(generation_id))
    )


def _choose_model(db: Session, model_id: str | None) -> str:
    routed = model_router.route(
        task="text_to_image",
        modality="image",
        explicit_model_id=model_id,
        db=db,
    )
    model = registry.get_model(routed["model_id"], db)
    if model.get("modality") != "image":
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Model {routed['model_id']} is not an image model.",
        )
    return routed["model_id"]


def _character_dir(character: Character) -> str:
    """Relative directory prefix for a character's generated images."""
    if character.project_id is None:
        return f"library/characters/{character.id}"
    return f"projects/{character.project_id}/characters/{character.id}"


def enqueue_master(
    db: Session,
    character: Character,
    prompt: str | None,
    model_id: str | None,
    project_id: uuid.UUID,
) -> uuid.UUID:
    """Queue the master portrait generation for a character.

    The master is the character's representative face/body image (D-18).
    Global characters generate inside the requesting project's context.
    """
    chosen_model = _choose_model(db, model_id)
    from app.services import budget_guard, cost_estimator, provider_input

    # ADR-0014: seed drawn AND persisted at enqueue (reproducible), resolved
    # provider inputs snapshotted; capability pre-check before the budget.
    seed = provider_input.draw_seed()
    snapshot = provider_input.image_snapshot(
        db, model_id=chosen_model, task="text_to_image", seed=seed,
        seed_source="random", negative_prompt=character.negative_prompt,
    )
    cost = cost_estimator.estimate_images(db, chosen_model, 1)
    budget_guard.check_budget(db, project_id, estimated_new=cost)  # 403 BUDGET_EXCEEDED
    gen = Generation(
        id=uuid.uuid4(),
        project_id=project_id,
        job_type="text_to_image",
        status="queued",
        model_id=chosen_model,
        params={
            "_character_id": str(character.id),
            "_reference_role": "master",
            "_source": "generated",
            "_character_dir": _character_dir(character),
            "snapshot": snapshot,
        },
        prompt_final=prompt or character.appearance_prompt,
        negative_final=character.negative_prompt,
        seed=seed,
        estimated_cost_usd=cost,
    )
    db.add(gen)
    db.flush()
    _enqueue_image_job(db, gen.id)
    return gen.id


def enqueue_sheet(
    db: Session,
    character: Character,
    project_id: uuid.UUID,
    views: list[str] | None,
    expressions: list[str] | None,
    outfits: list[str] | None,
    poses: list[str] | None,
) -> tuple[list[uuid.UUID], Decimal]:
    """Queue character-sheet generation jobs (turnaround, expressions...).

    One job per requested item; each job writes one reference image and one
    CharacterReference row on completion. Returns (job_ids, estimated_cost_usd).
    """

    from app.services import budget_guard, cost_estimator, provider_input

    jobs: list[uuid.UUID] = []
    total = Decimal("0")

    chosen_model = _choose_model(db, None)
    # ADR-0014 / audit F08: the approved master (primary reference) is the
    # identity reference of every sheet job. Pre-check before the budget.
    master = (
        db.query(CharacterReference)
        .filter(
            CharacterReference.character_id == character.id,
            CharacterReference.is_primary.is_(True),
        )
        .order_by(CharacterReference.created_at.desc())
        .first()
    )
    identity = (
        [{"path": master.image_path, "role": "identity", "kind": "character",
          "ref_id": str(master.id)}]
        if master is not None else []
    )
    snapshot = provider_input.image_snapshot(
        db, model_id=chosen_model, task="text_to_image",
        seed=provider_input.draw_seed(), seed_source="random",
        negative_prompt=character.negative_prompt, references=identity,
    )
    unit_cost = cost_estimator.estimate_images(db, chosen_model, 1)
    item_count = (
        len(views or []) + len(expressions or []) + len(outfits or []) + len(poses or [])
    )
    # 403 BUDGET_EXCEEDED before any job is created (fail fast, no partial batch).
    budget_guard.check_budget(db, project_id, estimated_new=unit_cost * item_count)

    def _enqueue(role: str, item_prompt: str) -> None:
        nonlocal total
        total += unit_cost
        seed = provider_input.draw_seed()  # persisted per job (reproducible)
        gen = Generation(
            id=uuid.uuid4(),
            project_id=project_id,
            job_type="text_to_image",
            status="queued",
            model_id=chosen_model,
            params={
                "_character_id": str(character.id),
                "_reference_role": role,
                "_source": "sheet_derived",
                "_character_dir": _character_dir(character),
                "snapshot": provider_input.with_seed(snapshot, seed),
            },
            prompt_final=f"{character.appearance_prompt}, {item_prompt}",
            negative_final=character.negative_prompt,
            seed=seed,
            estimated_cost_usd=unit_cost,
        )
        db.add(gen)
        db.flush()
        _enqueue_image_job(db, gen.id)
        jobs.append(gen.id)

    for role in views or []:
        check_enum("reference_role", str(role))
        _enqueue(str(role), str(role).replace("_", " "))
    allowed = set(enum_values("reference_role"))
    for expr in expressions or []:
        role = f"expression_{expr}"
        if role not in allowed:
            role = f"expression_{str(expr).split('_')[-1]}"
            if role not in allowed:
                raise StudioError(ErrorCode.VALIDATION_ERROR, f"Unknown expression: {expr}.")
        _enqueue(role, f"{expr} expression")
    for outfit in outfits or []:
        role = f"outfit_{outfit}"
        if role not in allowed:
            role = "outfit_alt"
        _enqueue(role, f"{outfit} outfit")
    for pose in poses or []:
        role = "pose_action"
        _enqueue(role, f"{pose} pose")
    return jobs, total


def reference_for_job(
    db: Session, generation: Generation, image_rel_path: str
) -> CharacterReference:
    """Create the CharacterReference row after a character generation completes.

    Called by the worker post-processing (tasks.post_generation). Keeps the
    pipeline contract in one place.
    """
    params = generation.params or {}
    character_id = uuid.UUID(str(params["_character_id"]))
    role = str(params["_reference_role"])
    source = str(params["_source"])
    character = db.get(Character, character_id)
    if character is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Character for generation not found.")

    # ADR-0005: a regenerated master demotes the previous primary instead of
    # colliding with the one-primary-per-character partial unique index.
    is_primary = role == "master"
    if is_primary:
        for old in (
            db.query(CharacterReference)
            .filter(
                CharacterReference.character_id == character_id,
                CharacterReference.is_primary.is_(True),
            )
            .all()
        ):
            old.is_primary = False

    ref = CharacterReference(
        id=uuid.uuid4(),
        character_id=character_id,
        image_path=image_rel_path,
        role=role,
        is_primary=is_primary,
        source=source,
    )
    db.add(ref)
    db.flush()
    return ref
