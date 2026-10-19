"""Scene / shot / shot-reference CRUD (P0-9, design §4.4–§4.6, §8.1).

Rules:
- Shots are ordered per scene (order_index, unique per scene).
- The first shot of a scene with no explicit shot_type is prefilled
  "establishing" (rule from the board screen design).
- Shot references must point at exactly one target (character XOR asset) and
  use one of the shot_reference_purpose contract enums.
- Board cards get a thumbnail (approved frame, else latest take) and a
  candidate count so the router stays thin.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.models.generation import Generation
from app.models.production import Scene, Shot, ShotReference


def _project_or_404(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def get_scene(db: Session, scene_id: uuid.UUID) -> Scene:
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Scene not found.")
    return scene




def get_shot(db: Session, shot_id: uuid.UUID) -> Shot:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    return shot



def _validate_shot_camera_fields(db: Session, fields: dict[str, Any]) -> None:
    """P1-2: validate camera_preset_id (404) and camera_side (left|right|center)."""
    from app.services.camera import CAMERA_SIDES, get_preset

    if fields.get("camera_preset_id") is not None:
        get_preset(db, fields["camera_preset_id"])
    side = fields.get("camera_side")
    if side is not None and side not in CAMERA_SIDES:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"camera_side must be one of {list(CAMERA_SIDES)}.",
        )




# --- scenes ---------------------------------------------------------------


def list_scenes(db: Session, project_id: uuid.UUID) -> list[Scene]:
    _project_or_404(db, project_id)
    return (
        db.query(Scene)
        .filter(Scene.project_id == project_id)
        .order_by(Scene.order_index)
        .all()
    )


def create_scene(db: Session, project_id: uuid.UUID, fields: dict[str, Any]) -> Scene:
    _project_or_404(db, project_id)
    max_idx = (
        db.query(Scene.order_index)
        .filter(Scene.project_id == project_id)
        .order_by(Scene.order_index.desc())
        .first()
    )
    scene = Scene(
        project_id=project_id,
        order_index=(max_idx[0] + 1) if max_idx else 0,
        **{k: v for k, v in fields.items() if k != "project_id"},
    )
    db.add(scene)
    db.flush()
    return scene


def update_scene(db: Session, scene_id: uuid.UUID, fields: dict[str, Any]) -> Scene:
    scene = get_scene(db, scene_id)
    for key, value in fields.items():
        if hasattr(scene, key):
            setattr(scene, key, value)
    db.flush()
    return scene


def delete_scene(db: Session, scene_id: uuid.UUID) -> None:
    scene = get_scene(db, scene_id)
    db.delete(scene)
    db.flush()


# --- shots ----------------------------------------------------------------


def list_shots(db: Session, scene_id: uuid.UUID) -> list[Shot]:
    get_scene(db, scene_id)
    return (
        db.query(Shot)
        .filter(Shot.scene_id == scene_id)
        .order_by(Shot.order_index)
        .all()
    )


def create_shot(db: Session, scene_id: uuid.UUID, fields: dict[str, Any]) -> Shot:
    scene = get_scene(db, scene_id)
    max_idx = (
        db.query(Shot.order_index)
        .filter(Shot.scene_id == scene_id)
        .order_by(Shot.order_index.desc())
        .first()
    )
    order_index = (max_idx[0] + 1) if max_idx else 0
    fields = dict(fields)
    if order_index == 0 and not fields.get("shot_type"):
        fields["shot_type"] = "establishing"
    if fields.get("shot_type"):
        check_enum("shot_type", fields["shot_type"])
    if fields.get("camera_angle"):
        check_enum("camera_angle", fields["camera_angle"])
    if fields.get("movement"):
        check_enum("camera_movement", fields["movement"])
    _validate_shot_camera_fields(db, fields)
    shot = Shot(scene_id=scene.id, order_index=order_index, **fields)
    db.add(shot)
    db.flush()
    return shot


def update_shot(db: Session, shot_id: uuid.UUID, fields: dict[str, Any]) -> Shot:
    shot = get_shot(db, shot_id)
    if "shot_type" in fields and fields["shot_type"]:
        check_enum("shot_type", fields["shot_type"])
    if "camera_angle" in fields and fields["camera_angle"]:
        check_enum("camera_angle", fields["camera_angle"])
    if "movement" in fields and fields["movement"]:
        check_enum("camera_movement", fields["movement"])
    _validate_shot_camera_fields(db, fields)
    changed_prompt_fields = [
        key
        for key in (
            "shot_type", "camera_angle", "movement", "action", "dialogue",
            "emotion_weights", "style_kit_id", "camera_preset_id", "camera_side",
        )
        if key in fields and fields[key] != getattr(shot, key)
    ]
    for key, value in fields.items():
        if hasattr(shot, key):
            setattr(shot, key, value)
    if changed_prompt_fields:
        # Prompt-relevant edits invalidate previously generated frames (F-06).
        has_takes = (
            db.query(Generation)
            .filter(Generation.shot_id == shot.id, Generation.is_batch.is_(False))
            .count()
            > 0
        )
        if has_takes:
            shot.frame_stale = True
    db.flush()
    return shot


def delete_shot(db: Session, shot_id: uuid.UUID) -> None:
    shot = get_shot(db, shot_id)
    db.delete(shot)
    db.flush()


def reorder_shots(db: Session, scene_id: uuid.UUID, ordered_ids: list[uuid.UUID]) -> list[Shot]:
    """Assign order_index by list position. All ids must belong to the scene."""
    get_scene(db, scene_id)
    shots = {s.id: s for s in list_shots(db, scene_id)}
    for idx, shot_id in enumerate(ordered_ids):
        shot = shots.get(shot_id)
        if shot is None:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                "ordered_ids contains a shot outside this scene.",
                details={"shot_id": str(shot_id)},
            )
        shot.order_index = idx
    db.flush()
    return list_shots(db, scene_id)


def duplicate_shot(db: Session, shot_id: uuid.UUID) -> Shot:
    """Copy prompt-relevant fields and references to a new trailing shot."""
    src = get_shot(db, shot_id)
    copy_fields = {
        "shot_type": src.shot_type,
        "camera_angle": src.camera_angle,
        "movement": src.movement,
        "duration_sec": src.duration_sec,
        "action": src.action,
        "dialogue": src.dialogue,
        "emotion_weights": src.emotion_weights,
        "sfx_notes": src.sfx_notes,
        "intended_text": src.intended_text,
        "style_kit_id": src.style_kit_id,
    }
    clone = create_shot(db, src.scene_id, copy_fields)
    for ref in db.query(ShotReference).filter(ShotReference.shot_id == src.id).all():
        db.add(
            ShotReference(
                shot_id=clone.id,
                character_id=ref.character_id,
                asset_id=ref.asset_id,
                purpose=ref.purpose,
                reference_detail_id=ref.reference_detail_id,
            )
        )
    db.flush()
    return clone


# --- shot references --------------------------------------------------------


def list_references(db: Session, shot_id: uuid.UUID) -> list[ShotReference]:
    get_shot(db, shot_id)
    return db.query(ShotReference).filter(ShotReference.shot_id == shot_id).all()


def add_reference(
    db: Session,
    shot_id: uuid.UUID,
    *,
    character_id: uuid.UUID | None,
    asset_id: uuid.UUID | None,
    purpose: str,
    reference_detail_id: uuid.UUID | None,
) -> ShotReference:
    shot = get_shot(db, shot_id)
    check_enum("shot_reference_purpose", purpose)
    if bool(character_id) == bool(asset_id):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "A shot reference must point at exactly one of character_id, asset_id.",
        )
    if character_id is not None:
        from app.models.character import Character

        if db.get(Character, character_id) is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Character not found.")
    if asset_id is not None:
        from app.models.asset import Asset

        if db.get(Asset, asset_id) is None:
            raise StudioError(ErrorCode.NOT_FOUND, "Asset not found.")
    ref = ShotReference(
        shot_id=shot.id,
        character_id=character_id,
        asset_id=asset_id,
        purpose=purpose,
        reference_detail_id=reference_detail_id,
    )
    db.add(ref)
    db.flush()
    return ref


def delete_reference(db: Session, reference_id: uuid.UUID) -> None:
    ref = db.get(ShotReference, reference_id)
    if ref is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot reference not found.")
    db.delete(ref)
    db.flush()


# --- board enrichment -------------------------------------------------------


def _thumbnail_for(db: Session, shot: Shot) -> str | None:
    """Approved frame first, else the latest succeeded take."""
    if shot.approved_frame_generation_id:
        gen = db.get(Generation, shot.approved_frame_generation_id)
        if gen is not None and gen.status == "succeeded" and gen.output_path:
            return gen.output_thumbnail_path or gen.output_path
    latest = (
        db.query(Generation)
        .filter(
            Generation.shot_id == shot.id,
            Generation.is_batch.is_(False),
            Generation.status == "succeeded",
        )
        .order_by(Generation.finished_at.desc().nullslast(), Generation.created_at.desc())
        .first()
    )
    if latest is not None and latest.output_path:
        return latest.output_thumbnail_path or latest.output_path
    return None


def shot_card(db: Session, shot: Shot) -> dict[str, Any]:
    """Board-card payload: the shot plus thumbnail and candidate count."""
    candidates = (
        db.query(Generation)
        .filter(Generation.shot_id == shot.id, Generation.is_batch.is_(False))
        .count()
    )
    return {
        "id": shot.id,
        "scene_id": shot.scene_id,
        "order_index": shot.order_index,
        "shot_type": shot.shot_type,
        "camera_angle": shot.camera_angle,
        "movement": shot.movement,
        "duration_sec": shot.duration_sec,
        "action": shot.action,
        "dialogue": shot.dialogue,
        "emotion_weights": shot.emotion_weights,
        "sfx_notes": shot.sfx_notes,
        "intended_text": shot.intended_text,
        "status": shot.status,
        "frame_stale": shot.frame_stale,
        "approved_frame_generation_id": shot.approved_frame_generation_id,
        "approved_at": shot.approved_at,
        "style_kit_id": shot.style_kit_id,
        "seed": shot.seed,
        "camera_preset_id": shot.camera_preset_id,
        "camera_side": shot.camera_side,
        "thumbnail": _thumbnail_for(db, shot),
        "candidate_count": candidates,
        "created_at": shot.created_at,
        "updated_at": shot.updated_at,
    }


def reference_card(db: Session, ref: ShotReference) -> dict[str, Any]:
    """Reference payload with denormalized target name and preview image."""
    name = image = None
    if ref.character_id is not None:
        from app.models.character import Character

        char = db.get(Character, ref.character_id)
        if char is not None:
            name = char.name
    elif ref.asset_id is not None:
        from app.models.asset import Asset

        asset = db.get(Asset, ref.asset_id)
        if asset is not None:
            name = asset.name
            image = asset.image_path
    return {
        "id": ref.id,
        "shot_id": ref.shot_id,
        "character_id": ref.character_id,
        "asset_id": ref.asset_id,
        "purpose": ref.purpose,
        "reference_detail_id": ref.reference_detail_id,
        "target_name": name,
        "target_image": image,
        "created_at": ref.created_at,
    }
