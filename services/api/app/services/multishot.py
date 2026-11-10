"""Multishot coverage (P1-1, design F-09, §4.18, §6.4, §8.1).

Flow:
  1. POST /shots/{id}/multishot -> request_coverage(): validates the base shot
     is frame_approved, checks budget (per_image x 6), locks the scene state
     into a MultishotSet row + 6 MultishotFrame rows, and dispatches the
     q.p1.run_multishot orchestrator (task arg = the set id, ADR-0008).
  2. run_multishot() (worker): claims the set (conditional UPDATE
     queued -> running as the FIRST statement of its transaction) and, in that
     same transaction, inserts 6 angle shots right after the base shot,
     assembles each angle's prompt (base prompt + angle instruction), creates
     one text_to_image generation per angle and links it to its frame with a
     write-once UPDATE (plan D9, RH2). Children are dispatched on q.image only
     after the commit. A duplicate/redelivered message finds the set no
     longer queued and is a no-op, so the fan-out happens exactly once;
     reconcile only re-sends existing queued children and re-aggregates.
  3. The image worker calls on_frame_finished() per child (hooked in
     tasks_image); aggregate_set() locks the set row and aggregates it from
     ALL frames to done | partial | failed, publishing job.progress /
     job.completed for the set id after the commit. Reconcile's reaggregate()
     reflects lost hooks into frames and always re-aggregates (B4B-H5).

The six angles and their shot_type mapping (contract enums):
  establishing -> establishing, wide -> wide, medium -> medium,
  medium_closeup -> close_up, closeup -> close_up, insert -> insert.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import dispatch_after_commit
from app.models.multishot import COVERAGE_ANGLES, MULTISHOT_STATUSES, MultishotFrame, MultishotSet
from app.services import budget_guard, cost_estimator, provider_input

ANGLE_SHOT_TYPE: dict[str, str] = {
    "establishing": "establishing",
    "wide": "wide",
    "medium": "medium",
    "medium_closeup": "close_up",
    "closeup": "close_up",
    "insert": "insert",
}

ANGLE_INSTRUCTION: dict[str, str] = {
    "establishing": (
        "Ultra-wide establishing framing: show the full location and environment, "
        "characters small in frame."
    ),
    "wide": "Wide shot framing: full bodies of the characters with surrounding space.",
    "medium": "Medium shot framing: characters from the waist up.",
    "medium_closeup": "Medium close-up framing: characters from the chest up.",
    "closeup": "Close-up framing: the face fills the frame, fine facial detail.",
    "insert": (
        "Insert shot: an extreme detail of one key object or action from the scene."
    ),
}


def _get_shot(db: Session, shot_id: uuid.UUID):
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    return shot


def _check_queue_full(db: Session) -> None:
    from app.models.generation import Generation

    pending = db.query(Generation).filter(Generation.status == "queued").count()
    if pending > defaults.QUEUE_MAX_PENDING:
        raise StudioError(
            ErrorCode.QUEUE_FULL,
            "Job queue is full; try again later.",
            retryable=True,
            details={"retry_after_sec": defaults.QUEUE_FULL_RETRY_AFTER_SEC},
        )


def _resolve_image_model(db: Session, shot, explicit_model_id: str | None) -> str:
    """explicit -> project default (image) -> router rule (§3.3)."""
    from app.models.production import Scene
    from app.models.project import Project
    from app.models_lib import router

    project_default = None
    scene = db.get(Scene, shot.scene_id)
    if scene is not None:
        project = db.get(Project, scene.project_id)
        if project is not None:
            project_default = (project.default_models or {}).get("image")
    routed = router.route(
        task="text_to_image",
        modality="image",
        explicit_model_id=explicit_model_id,
        project_default_model_id=project_default,
        db=db,
    )
    return str(routed["model_id"])


def _project_id_of(db: Session, shot) -> uuid.UUID:
    from app.models.production import Scene

    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Scene not found for shot.")
    return scene.project_id


def _state_snapshot(db: Session, shot, model_id: str) -> dict[str, Any]:
    """Lock the scene state the coverage is generated from (design F-09)."""
    from app.models.production import Scene, ShotReference
    from app.models.style import StyleKit

    scene = db.get(Scene, shot.scene_id)
    bindings = [
        {"character_id": str(r.character_id), "asset_id": str(r.asset_id) if r.asset_id else None,
         "purpose": r.purpose}
        for r in db.query(ShotReference).filter(ShotReference.shot_id == shot.id).all()
    ]
    kit = db.get(StyleKit, shot.style_kit_id) if shot.style_kit_id else None
    return {
        "model_id": model_id,
        "style_kit": {"id": str(kit.id), "version": kit.version} if kit else None,
        "character_bindings": bindings,
        "scene": {
            "location": scene.location if scene else None,
            "time_of_day": scene.time_of_day if scene else None,
            "mood": scene.mood if scene else None,
            "lighting": scene.lighting if scene else None,
        },
        "base_shot_id": str(shot.id),
    }


def _publish_progress(set_id: uuid.UUID, progress_pct: float, stage: str, status: str) -> None:
    get_bridge().publish(
        CHANNEL,
        "job.progress",
        {
            "job_id": str(set_id),
            "job_type": "multishot",
            "status": status,
            "progress_pct": progress_pct,
            "stage": stage,
        },
    )


def request_coverage(
    db: Session, shot_id: uuid.UUID, *, model_id: str | None = None
) -> dict[str, Any]:
    """Validate + create the set, then hand off to the q.p1.run_multishot worker."""
    shot = _get_shot(db, shot_id)
    # F-09: coverage is generated from an approved frame only.
    if shot.status != "frame_approved" or shot.approved_frame_generation_id is None:
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            "Multishot coverage requires an approved frame on the base shot.",
        )
    _check_queue_full(db)

    resolved_model = _resolve_image_model(db, shot, model_id)
    project_id = _project_id_of(db, shot)
    per_image = cost_estimator.estimate_images(db, resolved_model, 1)
    total = per_image * len(COVERAGE_ANGLES)
    budget_guard.check_budget(db, project_id, estimated_new=total)

    _check_status("queued")
    mset = MultishotSet(
        shot_id=shot.id,
        source_frame_generation_id=shot.approved_frame_generation_id,
        state_snapshot=_state_snapshot(db, shot, resolved_model),
        status="queued",
        dispatched_at=datetime.now(timezone.utc),
    )
    db.add(mset)
    db.flush()
    for angle in COVERAGE_ANGLES:
        db.add(MultishotFrame(multishot_set_id=mset.id, coverage_type=angle, status="pending"))
    db.flush()

    dispatch_after_commit(
        db, lambda: dispatch_job("q.p1.run_multishot", str(mset.id))
    )
    return {
        "multishot_set_id": mset.id,
        "job_id": mset.id,
        "estimated_cost_usd": total,
    }


def _check_status(value: str) -> str:
    # contracts/enums.json has no multishot_status entry; validate against
    # the module contract instead of touching the shared contract file.
    if value not in MULTISHOT_STATUSES:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, f"Invalid multishot status: {value}."
        )
    return value


def run_multishot(db: Session, multishot_set_id: str) -> dict[str, Any]:
    """Worker: claim the set, insert the 6 angle shots and queue one frame
    job per angle — all in the caller's ONE transaction (plan D9, RH2).

    The claim is the first statement: on Postgres it row-locks the set, so a
    concurrent duplicate delivery blocks, re-evaluates `status='queued'`
    after this commit and gets 0 rows; SQLite serializes the writers the
    same way. Each frame link is a write-once UPDATE (`generation_id IS
    NULL`), backed by UNIQUE(generation_id)/UNIQUE(shot_id): if anything
    would fan out twice the whole transaction rolls back. A crash before the
    commit leaves the set queued with no children, so redelivery/reconcile
    redoes the single fan-out safely. Dispatch and SSE happen after commit.
    """
    from app.models.generation import Generation
    from app.models.production import Scene, Shot, ShotReference
    from app.services import prompt_assembly

    set_id = uuid.UUID(multishot_set_id)
    # Budget lock BEFORE the claim (lock order Project -> set; B5 review H2):
    # the queued set's reservation turns into leaf frame jobs in this
    # transaction, and a budget check must never read between the two forms.
    project_id_row = (
        db.query(Scene.project_id)
        .join(Shot, Shot.scene_id == Scene.id)
        .join(MultishotSet, MultishotSet.shot_id == Shot.id)
        .filter(MultishotSet.id == set_id)
        .first()
    )
    if project_id_row is not None:
        budget_guard.lock_project(db, project_id_row[0])
    claimed = db.execute(
        update(MultishotSet)
        .where(MultishotSet.id == set_id, MultishotSet.status == "queued")
        .values(status=_check_status("running"))
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    mset = db.get(MultishotSet, set_id, populate_existing=True)
    if mset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Multishot set not found.")
    if not claimed:
        return {"multishot_set_id": str(mset.id), "status": mset.status, "resumed": True}

    base = _get_shot(db, mset.shot_id)
    scene = db.get(Scene, base.scene_id)
    project_id = scene.project_id if scene else _project_id_of(db, base)
    model_id = str((mset.state_snapshot or {}).get("model_id") or _resolve_image_model(db, base, None))
    per_image = cost_estimator.estimate_images(db, model_id, 1)
    now = datetime.now(timezone.utc)

    # Make room: shift later shots so the 6 coverage shots land right after base.
    db.query(Shot).filter(
        Shot.scene_id == base.scene_id, Shot.order_index > base.order_index
    ).update({Shot.order_index: Shot.order_index + len(COVERAGE_ANGLES)},
             synchronize_session=False)

    base_refs = (
        db.query(ShotReference).filter(ShotReference.shot_id == base.id).all()
    )
    frames = (
        db.query(MultishotFrame)
        .filter(MultishotFrame.multishot_set_id == mset.id)
        .order_by(MultishotFrame.created_at)
        .all()
    )
    by_angle = {f.coverage_type: f for f in frames}

    # Registered BEFORE the child dispatches (B4B-M3): callbacks run in order,
    # and a child that runs synchronously at dispatch (eager) may already
    # complete the set; its job.completed must stay the last event.
    dispatch_after_commit(
        db, lambda sid=mset.id: _publish_progress(sid, 30.0, "frames_queued", "running")
    )
    job_ids: list[str] = []
    for i, angle in enumerate(COVERAGE_ANGLES):
        frame = by_angle[angle]
        action = (base.action + "\n" if base.action else "") + (
            f"[Coverage angle: {angle}] {ANGLE_INSTRUCTION[angle]}"
        )
        shot = Shot(
            scene_id=base.scene_id,
            order_index=base.order_index + 1 + i,
            shot_type=ANGLE_SHOT_TYPE[angle],
            camera_angle=base.camera_angle,
            movement=None,
            duration_sec=base.duration_sec,
            action=action,
            dialogue=None,
            emotion_weights=base.emotion_weights,
            sfx_notes=base.sfx_notes,
            intended_text=None,
            status="frame_generating",
            style_kit_id=base.style_kit_id,
            coverage_source_frame_id=frame.id,
        )
        # Validate the mapped shot_type against the contract enum.
        check_enum("shot_type", shot.shot_type)
        db.add(shot)
        db.flush()
        for ref in base_refs:
            db.add(
                ShotReference(
                    shot_id=shot.id,
                    character_id=ref.character_id,
                    asset_id=ref.asset_id,
                    purpose=ref.purpose,
                    reference_detail_id=ref.reference_detail_id,
                )
            )

        assembled = prompt_assembly.assemble_shot_prompt(db, shot.id, model_id=model_id)
        # ADR-0014: resolved references + persisted seed in the snapshot.
        seed = provider_input.draw_seed()
        snapshot = provider_input.image_snapshot(
            db, model_id=model_id, task="text_to_image", seed=seed, seed_source="random",
            negative_prompt=assembled["negative_final"], references=assembled["references"],
            style_kit_id=assembled["style_kit_id"],
            style_kit_version=assembled["style_kit_version"],
        )
        check_enum("job_type", "text_to_image")
        gen = Generation(
            project_id=project_id,
            shot_id=shot.id,
            job_type="text_to_image",
            model_id=model_id,
            prompt_final=assembled["prompt_final"],
            negative_final=assembled["negative_final"],
            reference_ids=assembled["reference_ids"],
            style_kit_id=assembled["style_kit_id"],
            style_kit_version=assembled["style_kit_version"],
            estimated_cost_usd=per_image,
            seed=seed,
            params={
                "_multishot_frame_id": str(frame.id),
                "truncated_sections": assembled["truncated_sections"],
                "snapshot": snapshot,
            },
            max_retries=defaults.RETRY_MAX_ATTEMPTS,
            dispatched_at=now,
            # The leaves carry the reservation the set was approved under
            # (its budget period), not the fan-out time: a set queued last
            # month must not move its cost into this month unchecked
            # (review B5R2-H1).
            reserved_at=mset.created_at,
        )
        db.add(gen)
        db.flush()
        # Write-once link: a frame that already has a child aborts the whole
        # fan-out (rollback) instead of adding a second set of rows.
        linked = db.execute(
            update(MultishotFrame)
            .where(
                MultishotFrame.id == frame.id,
                MultishotFrame.generation_id.is_(None),
                MultishotFrame.shot_id.is_(None),
            )
            .values(shot_id=shot.id, generation_id=gen.id)
            .execution_options(synchronize_session=False)
        ).rowcount
        if linked != 1:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "Multishot frame already has a coverage job; fan-out refused.",
                details={"multishot_set_id": str(mset.id), "frame_id": str(frame.id)},
            )
        job_ids.append(str(gen.id))
        dispatch_after_commit(
            db, lambda gid=str(gen.id): dispatch_job("q.image.run_generation", gid)
        )

    db.flush()
    return {"multishot_set_id": str(mset.id), "status": "running", "job_ids": job_ids}


_FRAME_TERMINAL = ("done", "failed")


def reaggregate(db: Session, set_id: uuid.UUID) -> bool:
    """Reconcile helper (B4B-H5): reflect terminal child generations into
    frames whose per-frame hook was lost (frame still pending), then ALWAYS
    re-aggregate the set from the full frame state — also when every frame
    is already terminal but the set stayed `running` (two last hooks raced).
    Never creates rows. Returns True when the set reached a terminal status
    in this call."""
    from app.models.generation import Generation

    mset = db.get(MultishotSet, set_id)
    if mset is None or mset.status != "running":
        return False
    pending = (
        db.query(MultishotFrame.id, MultishotFrame.generation_id)
        .filter(MultishotFrame.multishot_set_id == set_id,
                MultishotFrame.status == "pending",
                MultishotFrame.generation_id.is_not(None))
        .order_by(MultishotFrame.id)  # one frame order for every writer
        .all()
    )
    reflected = 0
    for frame_id, generation_id in pending:
        status = db.query(Generation.status).filter(
            Generation.id == generation_id).scalar()
        if status not in ("succeeded", "failed", "canceled"):
            continue
        # Conditional: a hook that wrote this frame meanwhile wins (0 rows).
        reflected += db.execute(
            update(MultishotFrame)
            .where(MultishotFrame.id == frame_id, MultishotFrame.status == "pending")
            .values(status="done" if status == "succeeded" else "failed")
            .execution_options(synchronize_session=False)
        ).rowcount
    return aggregate_set(db, set_id, announce=reflected > 0)


def aggregate_set(db: Session, set_id: uuid.UUID, *, announce: bool = True) -> bool:
    """Aggregate a set's status from ALL its frames under the set row lock.

    The lock (SELECT ... FOR NO KEY UPDATE; SQLite serializes writers) is
    taken BEFORE the frames are read and is held until the caller's commit,
    so of two frame hooks committing concurrently the later one always sees
    the earlier one's frame (B4B-H5). Lock order: frame row (already
    written by the caller) -> set row (ADR-0013).

    SSE (after commit): with `announce` (a frame changed in this
    transaction) job.progress for a running set and job.completed when every
    frame is terminal; without it (reconcile found nothing new) only a
    job.completed for a status that actually changed.
    Returns True when the set moved from running to a terminal status.
    """
    from app.services.generations import lock_row

    mset = lock_row(db, MultishotSet, set_id)
    if mset is None or mset.status == "queued":
        return False
    states = [
        status for (status,) in db.query(MultishotFrame.status).filter(
            MultishotFrame.multishot_set_id == set_id)
    ]
    if not states:
        return False
    done_count = sum(1 for s in states if s == "done")
    was = mset.status
    if was == "running" and announce:
        pct = round(100.0 * done_count / len(states), 1)
        dispatch_after_commit(db, lambda: _publish_progress(set_id, pct, "frames_done", "running"))
    if not all(s in _FRAME_TERMINAL for s in states):
        return False
    if done_count == len(states):
        status = "done"
    elif done_count:
        status = "partial"
    else:
        status = "failed"
    mset.status = _check_status(status)
    db.flush()
    if announce or status != was:
        dispatch_after_commit(db, lambda: get_bridge().publish(
            CHANNEL,
            "job.completed",
            {
                "job_id": str(set_id),
                "job_type": "multishot",
                "status": status,
                "output_count": done_count,
            },
        ))
    return was == "running"


def fail_set(multishot_set_id: str, code: str, message: str) -> bool:
    """Persist a crashed fan-out (own committed transaction, plan D9).

    The worker transaction rolled back, so the set is still queued with no
    children; mark it (and its frames) failed so reconcile does not re-send a
    deterministic failure forever. Returns True when this call failed it.
    """
    from app.db.session import session_scope

    set_id = uuid.UUID(multishot_set_id)
    with session_scope() as db:
        result = db.execute(
            update(MultishotSet)
            .where(MultishotSet.id == set_id, MultishotSet.status == "queued")
            .values(status=_check_status("failed"))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        db.execute(
            update(MultishotFrame)
            .where(MultishotFrame.multishot_set_id == set_id,
                   MultishotFrame.status == "pending")
            .values(status="failed")
            .execution_options(synchronize_session=False)
        )
    get_bridge().publish(
        CHANNEL,
        "job.failed",
        {"job_id": multishot_set_id, "job_type": "multishot", "status": "failed",
         "error_code": code, "error_message": message, "retryable": False,
         "retry_count": 0, "max_retries": 0},
    )
    return True


def on_frame_finished(db: Session, frame_id: str, success: bool) -> None:
    """Called from the image worker when a coverage frame reaches terminal
    state (inside the child's terminal transaction): write the frame, then
    aggregate the set under its row lock."""
    frame = db.get(MultishotFrame, uuid.UUID(frame_id))
    if frame is None:
        return
    frame.status = "done" if success else "failed"
    db.flush()
    aggregate_set(db, frame.multishot_set_id, announce=True)


def approve_frame(db: Session, frame_id: uuid.UUID) -> MultishotFrame:
    """Per-cut approval (F-09). The shot board keeps its own approval flow;
    this flag marks the coverage cut as accepted inside the set."""
    frame = db.get(MultishotFrame, frame_id)
    if frame is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Multishot frame not found.")
    frame.approved = True
    db.flush()
    return frame


def get_set(db: Session, set_id: uuid.UUID) -> MultishotSet:
    mset = db.get(MultishotSet, set_id)
    if mset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Multishot set not found.")
    return mset


def list_frames(db: Session, set_id: uuid.UUID) -> list[MultishotFrame]:
    return (
        db.query(MultishotFrame)
        .filter(MultishotFrame.multishot_set_id == set_id)
        .order_by(MultishotFrame.created_at)
        .all()
    )
