"""Shot approval (D-13, D-26).

Approval is an explicit user button action — never automatic. The approval
target is always a batch *child* generation id (D-26): approving a batch
parent is rejected because the chosen take would be ambiguous.

Error-code mapping note: the 14 contract codes have no generic "conflict"
code, so approval-state violations on this endpoint use 409
APPROVAL_REQUIRED with a `details.reason` discriminator (the design's §8.1
row for this endpoint specifies 409 for transition violations).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError

_APPROVABLE_FROM = ("frame_done", "frame_approved")


def approve_frame(db: Session, shot_id: uuid.UUID, generation_id: uuid.UUID):
    """Approve one take as the shot's final frame.

    Returns the updated Shot. Raises:
    - 404 NOT_FOUND when the shot or generation does not exist,
    - 400 VALIDATION_ERROR when the generation is a batch parent (D-26) or
      belongs to another shot,
    - 409 APPROVAL_REQUIRED when the generation is not a succeeded image or
      the shot is not in an approvable state.
    """
    from app.models.generation import Generation
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    gen = db.get(Generation, generation_id)
    if gen is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Generation not found.")
    if gen.shot_id != shot.id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Generation does not belong to this shot.",
            details={"generation_id": str(generation_id), "shot_id": str(shot_id)},
        )
    if gen.is_batch:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Approve a batch child (take), not the batch parent.",
            details={"reason": "parent_not_child", "generation_id": str(generation_id)},
        )
    if gen.status != "succeeded" or not gen.output_path:
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            "Only a succeeded take with a saved image can be approved.",
            details={"reason": "take_not_succeeded", "status": gen.status},
        )
    if shot.status not in _APPROVABLE_FROM:
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            f"Shot cannot be approved from status '{shot.status}'.",
            details={"reason": "shot_not_ready", "status": shot.status},
        )

    check_enum("shot_status", "frame_approved")
    shot.approved_frame_generation_id = gen.id
    shot.approved_at = datetime.now(timezone.utc)
    shot.status = "frame_approved"
    db.flush()
    return shot


def require_approved_frame(db: Session, shot):
    """Return the approved Generation, or raise 409 APPROVAL_REQUIRED.

    Phase 3's video endpoint calls this before image-to-video; the review
    screen mirrors it by disabling the clip button for unapproved shots.

    The gate keys on the approved-frame *record* (approved_frame_generation_id
    pointing at a succeeded generation with an output file), not on the shot
    status alone: after clips exist (video_generating/video_done) the frame
    is still the approved one, so re-generation must stay allowed.
    """
    from app.models.generation import Generation

    if shot.status not in ("frame_approved", "video_generating", "video_done") or not shot.approved_frame_generation_id:
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            "Approve a frame before generating video.",
            details={"reason": "approval_required", "shot_id": str(shot.id)},
        )
    gen = db.get(Generation, shot.approved_frame_generation_id)
    if gen is None or gen.status != "succeeded" or not gen.output_path:
        raise StudioError(
            ErrorCode.APPROVAL_REQUIRED,
            "The approved frame is no longer available.",
            details={"reason": "approved_frame_missing", "shot_id": str(shot.id)},
        )
    return gen
