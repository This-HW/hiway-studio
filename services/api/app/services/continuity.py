"""Continuity generation source resolution (D-15).

Fallback chain for the "previous frame" input of continuity generation:
  1. previous clip's last frame — not available until Phase 3 (video);
  2. previous shot's approved still (`approved_frame_generation_id`);
  3. unavailable: continuity is offered as inactive with a reason, and the
     caller falls back to a normal generation (P0_SCREENS §7).

The "previous shot" is the shot immediately before in project order
(scene.order_index, then shot.order_index).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError


def previous_shot(db: Session, shot) -> Any | None:
    """Return the shot immediately before `shot` in project order."""
    from app.models.production import Scene, Shot

    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return None
    # Candidates: earlier shots in the same scene, or last shots of earlier scenes.
    same_scene = (
        db.query(Shot)
        .filter(Shot.scene_id == scene.id, Shot.order_index < shot.order_index)
        .order_by(Shot.order_index.desc())
        .first()
    )
    if same_scene is not None:
        return same_scene
    prev_scene = (
        db.query(Scene)
        .filter(Scene.project_id == scene.project_id, Scene.order_index < scene.order_index)
        .order_by(Scene.order_index.desc())
        .first()
    )
    if prev_scene is None:
        return None
    return (
        db.query(Shot)
        .filter(Shot.scene_id == prev_scene.id)
        .order_by(Shot.order_index.desc())
        .first()
    )


def _continuity_anchors(db: Session, shot) -> dict[str, str]:
    """ADR-0011 §9: 5-kind continuity anchors for multi-shot chains.

    Every shot in a continuous sequence must explicitly carry:
      1. identity — character identity clause (never re-described)
      2. wardrobe — outfit fragment
      3. move_direction — action's movement direction cue
      4. camera_behavior — shot_type/angle/movement phrase
      5. env_markers — two environment markers (location + time/lighting)
    """
    from app.models.character import Character, CharacterWardrobe
    from app.models.production import Scene, ShotReference

    anchors: dict[str, str] = {}
    scene = db.get(Scene, shot.scene_id) if shot.scene_id else None

    # 1. identity
    refs = (
        db.query(ShotReference)
        .filter(ShotReference.shot_id == shot.id, ShotReference.character_id.is_not(None))
        .all()
    )
    identities = []
    wardrobes = []
    for ref in refs:
        char = db.get(Character, ref.character_id)
        if char is None:
            continue
        identities.append(f"{char.name}: {char.appearance_prompt}")
        w = (
            db.query(CharacterWardrobe)
            .filter(
                CharacterWardrobe.character_id == char.id,
                CharacterWardrobe.is_default.is_(True),
            )
            .first()
        )
        if w is not None and w.prompt_fragment:
            wardrobes.append(f"{char.name}: {w.prompt_fragment}")
    if identities:
        anchors["identity"] = "; ".join(identities)
    # 2. wardrobe
    if wardrobes:
        anchors["wardrobe"] = "; ".join(wardrobes)
    # 3. move_direction (from the action text)
    if shot.action:
        anchors["move_direction"] = shot.action
    # 4. camera_behavior
    cam = [p for p in (shot.shot_type, shot.camera_angle, shot.movement) if p]
    if cam:
        anchors["camera_behavior"] = ", ".join(p.replace("_", " ") for p in cam)
    # 5. env_markers (exactly two: location + time/lighting)
    env = []
    if scene:
        if scene.location:
            env.append(scene.location)
        tl = scene.time_of_day or scene.lighting
        if tl:
            env.append(tl)
    if env:
        anchors["env_markers"] = ", ".join(env[:2])
    return anchors


def _state_summary(db: Session, shot) -> str:
    """One-line state summary: location, time of day, wardrobe, lighting (§7.1 #8)."""
    from app.models.character import Character
    from app.models.production import Scene, ShotReference

    scene = db.get(Scene, shot.scene_id)
    bits: list[str] = []
    if scene:
        if scene.location:
            bits.append(f"location: {scene.location}")
        if scene.time_of_day:
            bits.append(f"time: {scene.time_of_day}")
        if scene.lighting:
            bits.append(f"lighting: {scene.lighting}")
    refs = (
        db.query(ShotReference)
        .filter(ShotReference.shot_id == shot.id, ShotReference.character_id.is_not(None))
        .all()
    )
    names = []
    for ref in refs:
        char = db.get(Character, ref.character_id)
        if char is not None:
            names.append(char.name)
    if names:
        bits.append("characters: " + ", ".join(names))
    # ADR-0011 §9 anchors appended as an explicit checklist.
    anchors = _continuity_anchors(db, shot)
    for key in ("identity", "wardrobe", "move_direction", "camera_behavior", "env_markers"):
        if anchors.get(key):
            bits.append(f"anchor[{key}]: {anchors[key]}")
    return "; ".join(bits)


def get_continuity_source(db: Session, shot_id: uuid.UUID) -> dict[str, Any]:
    """Resolve the continuity source for a shot.

    Returns a dict with:
      available (bool), source_shot_id, source_generation_id, source_path
      (relative), summary (state line for the prompt), reason, step
      ("approved_still" | "unavailable").
    """
    from app.models.generation import Generation
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")

    prev = previous_shot(db, shot)
    if prev is None:
        return {
            "available": False,
            "step": "unavailable",
            "reason": "No previous shot exists; this is the first shot.",
            "summary": "",
        }

    # Step 1: previous clip's last frame — video arrives in Phase 3.
    # Step 2: approved still.
    if prev.approved_frame_generation_id:
        gen = db.get(Generation, prev.approved_frame_generation_id)
        if gen is not None and gen.status == "succeeded" and gen.output_path:
            return {
                "available": True,
                "step": "approved_still",
                "source_shot_id": str(prev.id),
                "source_generation_id": str(gen.id),
                "source_path": gen.output_path,
                "summary": _state_summary(db, prev),
                "reason": (
                    "Using the previous shot's approved still "
                    "(clip last-frame continuity arrives in Phase 3)."
                ),
            }
    return {
        "available": False,
        "step": "unavailable",
        "reason": (
            "The previous shot has no approved frame yet. "
            "Approve a frame on the previous shot to enable continuity."
        ),
        "summary": "",
        "source_shot_id": str(prev.id),
    }
