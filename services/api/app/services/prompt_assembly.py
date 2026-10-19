"""Prompt assembly pipeline (design report §7, ADR-0011 §1).

`assemble_shot_prompt(shot_id, model_id)` joins ordered slots with newlines
(§7.1), injects reference images by rule (§7.2), truncates over-long prompts
(§7.3), honors per-model slot order overrides (§7.4), and records
everything needed for reproducibility (§7.5).

Slot order (front slots hit harder; subject is 5-8 words up front):
  1. subject      — shot action + character identity clause (ADR-0011 §4:
                     never re-describe the face; identity comes from the
                     character reference image, not the prompt)
  2. setting      — scene location/time_of_day + category background cue
                     (+ continuity summary for continuous generation)
  3. style_medium — style kit texture/composition + category medium
                     cue (the medium/material concept lives in the kit's
                     `texture` field; the model has no `medium` column).
                     Never two conflicting styles.
  4. lighting     — style kit lighting + scene lighting (concrete sources)
  5. camera_lens  — shot_type/camera_angle/movement in natural language.
                     Movement must come from the 22-kind camera_movement
                     enum (contracts); free text is forbidden (ADR-0011 §8).
  6. color_mood   — style kit color + scene mood
  7. params       — model parameters, placed last
  —  negative     — separate field, never mixed into the main prompt.

Pure domain logic: no router or task imports. The worker reads the stored
`prompt_final`/`reference_ids` from the generation row; it never calls this.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core import defaults
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError

# Slot order (§7.1, ADR-0011 §1). `negative` is assembled separately and passed
# as its own field — it never mixes into the main prompt.
SLOT_ORDER = [
    "subject",
    "setting",
    "style_medium",
    "lighting",
    "camera_lens",
    "color_mood",
    "params",
]

# Reference injection priority (§7.2): primary > expression > outfit >
# location > continuity. Lower index survives truncation first.
_REF_PRIORITY = ["primary", "expression", "outfit", "location", "continuity"]

_SHOT_TYPE_PHRASE = {
    "establishing": "establishing wide shot",
    "wide": "wide shot",
    "medium": "medium shot",
    "close_up": "close-up shot",
    "extreme_close_up": "extreme close-up",
    "over_the_shoulder": "over-the-shoulder shot",
    "pov": "POV shot",
    "insert": "insert shot",
    "reaction": "reaction shot",
    "aerial": "aerial shot",
    "dutch": "dutch-angle shot",
}

_CAMERA_ANGLE_PHRASE = {
    "eye_level": "eye-level angle",
    "high": "high angle",
    "low": "low angle",
    "birds_eye": "bird's-eye view",
    "worms_eye": "worm's-eye view",
    "dutch": "dutch angle",
    "ots": "over-the-shoulder angle",
}

# All 22 camera_movement enum kinds (packages/contracts/enums.json).
# Free-text camera description is forbidden (ADR-0011 §8).
_CAMERA_MOVEMENT_PHRASE = {
    "static": "static camera",
    "pan": "slow pan",
    "tilt": "slow tilt",
    "dolly_in": "slow dolly-in",
    "dolly_out": "slow dolly-out",
    "truck": "trucking movement",
    "crane": "crane movement",
    "orbit": "orbital movement",
    "orbit_360": "full 360-degree orbit",
    "zoom_in": "slow zoom in",
    "zoom_out": "slow zoom out",
    "crash_zoom_in": "crash zoom in",
    "crash_zoom_out": "crash zoom out",
    "dolly_zoom_in": "dolly zoom in",
    "dolly_zoom_out": "dolly zoom out",
    "whip_pan": "whip pan",
    "jib_up": "jib up",
    "jib_down": "jib down",
    "snorricam": "snorricam rig",
    "hyperlapse": "hyperlapse",
    "handheld": "handheld camera",
    "fpv": "first-person movement",
}


def _get_shot(db: Session, shot_id: uuid.UUID):
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.", details={"shot_id": str(shot_id)})
    return shot


def _resolve_style_kit(db: Session, shot, scene):
    """Shot -> scene -> project default style kit (first non-null wins)."""
    from app.models.style import StyleKit

    for kit_id in (shot.style_kit_id, scene.style_kit_id if scene else None):
        if kit_id:
            kit = db.get(StyleKit, kit_id)
            if kit is not None:
                return kit
    return None


def _slot_style_medium(kit) -> str:
    """Slot 3: style medium — texture/composition (+ category medium cue).

    The StyleKit model has no `medium` column; the medium/material concept
    lives in `texture` (e.g. "35mm film grain, photographic", "oil painting").
    Keep the raw tokens verbatim when the kit is locked (ADR-0011 §13).
    """
    if kit is None:
        return ""
    parts = [kit.texture, kit.composition]
    present = [p for p in parts if p]
    if not present:
        return ""
    return "Style: " + ", ".join(present)


def _slot_lighting(kit, scene) -> str:
    """Slot 4: concrete light sources (kit + scene). Never truncated."""
    parts = []
    if kit is not None and kit.lighting:
        parts.append(kit.lighting)
    if scene is not None and scene.lighting:
        parts.append(scene.lighting)
    if not parts:
        return ""
    return "Lighting: " + ", ".join(parts)


def _slot_color_mood(kit, scene) -> str:
    """Slot 6: color grade + mood."""
    parts = []
    if kit is not None and kit.color:
        parts.append(kit.color)
    if scene is not None and scene.mood:
        parts.append(scene.mood)
    if not parts:
        return ""
    return "Color and mood: " + ", ".join(parts)


def _slot_category_medium(db: Session, project) -> tuple[str, str]:
    """Return (background_cue, medium_cue) from the category pack."""
    if not project or not project.category_pack_code:
        return "", ""
    from app.models.style import CategoryPack

    pack = db.get(CategoryPack, project.category_pack_code)
    if pack is None:
        return "", ""
    cfg = pack.pipeline_config or {}
    background = cfg.get("background_cue") or ""
    medium = cfg.get("medium_cue") or cfg.get("prompt_instruction") or ""
    return background, medium


def _shot_characters(db: Session, shot) -> list:
    """Characters referenced by the shot, in reference creation order."""
    from app.models.character import Character
    from app.models.production import ShotReference

    refs = (
        db.query(ShotReference)
        .filter(ShotReference.shot_id == shot.id, ShotReference.character_id.is_not(None))
        .order_by(ShotReference.created_at)
        .all()
    )
    chars = []
    for ref in refs:
        char = db.get(Character, ref.character_id)
        if char is not None:
            chars.append((ref, char))
    return chars


def _primary_reference(db: Session, character_id: uuid.UUID):
    from app.models.character import CharacterReference

    return (
        db.query(CharacterReference)
        .filter(
            CharacterReference.character_id == character_id,
            CharacterReference.is_primary.is_(True),
        )
        .order_by(CharacterReference.created_at.desc())
        .first()
    )


def _reference_by_role(db: Session, character_id: uuid.UUID, role: str):
    from app.models.character import CharacterReference

    return (
        db.query(CharacterReference)
        .filter(CharacterReference.character_id == character_id, CharacterReference.role == role)
        .order_by(CharacterReference.created_at.desc())
        .first()
    )


def _top_emotion(emotion_weights: dict | None) -> str | None:
    if not emotion_weights:
        return None
    try:
        return max(emotion_weights.items(), key=lambda kv: float(kv[1]))[0]
    except (ValueError, TypeError):
        return None


def assemble_shot_prompt(
    db: Session,
    shot_id: uuid.UUID,
    model_id: str | None = None,
    overrides: dict[str, str | None] | None = None,
    continuity_summary: str | None = None,
    continuity_image_path: str | None = None,
    continuity_source_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the final prompt for a shot (§7, ADR-0011 §1).

    Returns a dict with prompt_final, negative_final, reference_image_paths
    (relative), reference_ids (snapshot), style_kit_id/version,
    truncated_sections, and the per-slot texts.
    """
    from app.models.production import Scene, ShotReference
    from app.models.project import Project

    shot = _get_shot(db, shot_id)
    scene = db.get(Scene, shot.scene_id) if shot.scene_id else None
    project = db.get(Project, scene.project_id) if scene else None

    overrides = overrides or {}
    slots: dict[str, str] = {}
    slot_meta: dict[str, dict[str, str]] = {}

    kit = _resolve_style_kit(db, shot, scene)
    bg_cue, medium_cue = _slot_category_medium(db, project)

    # Slot 1: subject — action + character identity. The face is NEVER
    # re-described here; identity comes from the reference image and the
    # canonical identity clause (ADR-0011 §4).
    char_pairs = _shot_characters(db, shot)
    subject_lines: list[str] = []
    negatives: list[str] = []
    ref_images: list[tuple[str, str, str, str]] = []  # (priority, kind, rel_path, ref_id)

    shot_refs = db.query(ShotReference).filter(ShotReference.shot_id == shot.id).all()
    outfit_ref = next(
        (r for r in shot_refs if r.purpose == "outfit" and r.character_id), None
    )

    for ref, char in char_pairs:
        # Identity clause only — appearance_prompt is the canonical fixed
        # phrase from the CharacterPack, not a fresh face description.
        subject_lines.append(f"{char.name}: {char.appearance_prompt}")
        if char.negative_prompt:
            negatives.append(f"{char.name}: {char.negative_prompt}")
        primary = _primary_reference(db, char.id)
        if primary is not None:
            ref_images.append(("primary", "character", primary.image_path, str(primary.id)))
        # Wardrobe fragment belongs to the subject (what the character wears).
        from app.models.character import CharacterWardrobe

        wardrobe = None
        if outfit_ref is not None and outfit_ref.character_id == char.id and outfit_ref.reference_detail_id:
            wardrobe = db.get(CharacterWardrobe, outfit_ref.reference_detail_id)
        if wardrobe is None:
            wardrobe = (
                db.query(CharacterWardrobe)
                .filter(
                    CharacterWardrobe.character_id == char.id,
                    CharacterWardrobe.is_default.is_(True),
                )
                .first()
            )
        if wardrobe is not None and wardrobe.prompt_fragment:
            subject_lines.append(f"{char.name} wearing {wardrobe.prompt_fragment}")
            from app.models.character import CharacterReference as CR

            img_ref = (
                db.get(CR, wardrobe.reference_image_id)
                if wardrobe.reference_image_id
                else None
            )
            if img_ref is None:
                img_ref = _reference_by_role(db, char.id, "outfit_default")
            if img_ref is not None:
                ref_images.append(("outfit", "wardrobe", img_ref.image_path, str(img_ref.id)))
        elif wardrobe is None:
            fallback = _reference_by_role(db, char.id, "outfit_default")
            if fallback is not None:
                ref_images.append(("outfit", "wardrobe", fallback.image_path, str(fallback.id)))

    if shot.action:
        subject_lines.insert(0, f"{shot.action}")
    if shot.dialogue:
        emotion = _top_emotion(shot.emotion_weights)
        hint = f" ({emotion})" if emotion else ""
        subject_lines.append(f'Saying{hint}: "{shot.dialogue}"')
    slots["subject"] = "\n".join(subject_lines)

    # Expression reference (§7.2): top emotion for the first character.
    if char_pairs and (shot.dialogue or shot.emotion_weights):
        _, first_char = char_pairs[0]
        emotion = _top_emotion(shot.emotion_weights) or "neutral"
        role = f"expression_{emotion}"
        expr_ref = _reference_by_role(db, first_char.id, role)
        if expr_ref is None and role != "expression_neutral":
            expr_ref = _reference_by_role(db, first_char.id, "expression_neutral")
        if expr_ref is not None:
            ref_images.append(("expression", "expression", expr_ref.image_path, str(expr_ref.id)))

    # Slot 2: setting — location + time (+ continuity state for chains).
    setting_parts = []
    if scene:
        if scene.location:
            setting_parts.append(scene.location)
        if scene.time_of_day:
            setting_parts.append(scene.time_of_day)
    if bg_cue:
        setting_parts.append(bg_cue)
    if continuity_summary:
        setting_parts.append(f"Continuity: {continuity_summary}")
    slots["setting"] = "Setting: " + ", ".join(setting_parts) if setting_parts else ""
    if continuity_image_path:
        ref_images.append(("continuity", "continuity", continuity_image_path,
                           continuity_source_id or f"continuity:{continuity_image_path}"))

    # Location reference (§7.2): establishing shots pull the location asset.
    if shot.shot_type == "establishing":
        from app.models.asset import Asset

        loc_ref = next(
            (r for r in shot_refs if r.purpose == "location" and r.asset_id is not None),
            None,
        )
        if loc_ref is not None:
            asset = db.get(Asset, loc_ref.asset_id)
            if asset is not None and asset.image_path:
                ref_images.append(("location", "location", asset.image_path, str(asset.id)))

    # Slot 3: style/medium.
    style_medium = _slot_style_medium(kit)
    if medium_cue:
        style_medium = f"{style_medium}, {medium_cue}" if style_medium else f"Style: {medium_cue}"
    slots["style_medium"] = style_medium
    # Truncation keeps the core (first clause) of style_medium.
    slot_meta["style_medium"] = {"core": style_medium.split(",")[0] if style_medium else ""}

    # Slot 4: lighting (never truncated — core of shot intent).
    slots["lighting"] = _slot_lighting(kit, scene)

    # Slot 5: camera/lens — 22-kind enum, adjectives droppable.
    cam_core, cam_adj = [], []
    if shot.shot_type:
        cam_core.append(_SHOT_TYPE_PHRASE.get(shot.shot_type, shot.shot_type.replace("_", " ")))
    if shot.camera_angle:
        cam_adj.append(_CAMERA_ANGLE_PHRASE.get(shot.camera_angle, shot.camera_angle.replace("_", " ")))
    if shot.movement:
        check_enum("camera_movement", shot.movement)
        cam_adj.append(_CAMERA_MOVEMENT_PHRASE.get(shot.movement, shot.movement.replace("_", " ")))
    if kit is not None and getattr(kit, "lens", None):
        cam_adj.append(kit.lens)
    camera_full = "Camera: " + ", ".join(cam_core + cam_adj) if (cam_core or cam_adj) else ""
    camera_core = "Camera: " + ", ".join(cam_core) if cam_core else ""
    slots["camera_lens"] = camera_full
    slot_meta["camera_lens"] = {"core": camera_core}

    # Slot 6: color/mood (never truncated — core of shot intent).
    slots["color_mood"] = _slot_color_mood(kit, scene)

    # Slot 7: params — model-specific trailing tokens.
    slots["params"] = _slot_params(model_id, shot)

    # Apply overrides (replace or delete slots).
    for name, value in overrides.items():
        if name in slots:
            slots[name] = value or ""

    # Reference cap (§7.2): model capability, else [assumption] default 4.
    max_refs = _max_reference_images(model_id)
    ordered = sorted(ref_images, key=lambda t: _REF_PRIORITY.index(t[0]))
    seen_paths: set[str] = set()
    deduped: list[tuple[str, str, str, str]] = []
    for prio, kind, path, ref_id in ordered:
        if path not in seen_paths:
            seen_paths.add(path)
            deduped.append((prio, kind, path, ref_id))
    kept = deduped[:max_refs]
    ref_id_snapshot = [ref_id for _, _, _, ref_id in kept]
    reference_image_paths = [path for _, _, path, _ in kept]

    # Slot order override (§7.4).
    order = _slot_order(model_id)

    # Truncation (§7.3): 1. setting details, 2. style_medium details
    # (lighting/color core kept), 3. camera_lens adjectives.
    # subject / lighting / color_mood cores are never cut.
    max_chars = _max_prompt_chars(model_id)
    truncated: list[str] = []

    def _join() -> str:
        return "\n".join(slots[name] for name in order if slots.get(name))

    prompt = _join()
    if len(prompt) > max_chars:
        if slots.get("setting"):
            slots["setting"] = ""
            truncated.append("setting")
            prompt = _join()
    if len(prompt) > max_chars:
        core = slot_meta.get("style_medium", {}).get("core", "")
        if slots.get("style_medium") and core != slots["style_medium"]:
            slots["style_medium"] = core
            truncated.append("style_medium:details")
            prompt = _join()
    if len(prompt) > max_chars:
        core = slot_meta.get("camera_lens", {}).get("core", "")
        if slots.get("camera_lens") and core != slots["camera_lens"]:
            slots["camera_lens"] = core
            truncated.append("camera_lens:adjectives")
            prompt = _join()
    if len(prompt) > max_chars:
        raise StudioError(
            ErrorCode.PROMPT_TOO_LONG,
            "Prompt exceeds the model's max length even after truncation.",
            details={"length": len(prompt), "max_chars": max_chars},
        )

    # Negative (separate field, ADR-0011 §3: curated library sets).
    from app.services import prompt_snippets as snippet_service

    global_negative = snippet_service.global_negative_text(db)
    neg_parts = [global_negative] + negatives if global_negative else negatives
    negative_final = ", ".join(p for p in neg_parts if p)

    # Token budget advisory (ADR-0011 §2): SD1.5 77-token hard cap.
    estimated_tokens = estimate_tokens(prompt)
    token_budget = _max_prompt_tokens(model_id)
    token_warning = (
        token_budget is not None
        and estimated_tokens > token_budget
    )

    return {
        "prompt_final": prompt,
        "negative_final": negative_final,
        "reference_image_paths": reference_image_paths,
        "reference_ids": ref_id_snapshot,
        "style_kit_id": kit.id if kit else None,
        "style_kit_version": kit.version if kit else None,
        "truncated_sections": truncated,
        "sections": {k: v for k, v in slots.items()},
        "estimated_tokens": estimated_tokens,
        "token_budget": token_budget,
        "token_warning": token_warning,
    }


# Backwards-compatible alias (pre-§7.1-rewrite name).
def assemble_prompt(*args, **kwargs):
    """Deprecated: use assemble_shot_prompt."""
    return assemble_shot_prompt(*args, **kwargs)


def _slot_params(model_id: str | None, shot) -> str:
    """Slot 7: model-specific trailing parameter tokens."""
    caps = _model_caps(model_id)
    suffix = caps.get("prompt_params_suffix") or ""
    return suffix


def estimate_tokens(text: str) -> int:
    """Rough token estimate (ADR-0011 §2).

    No tokenizer is bundled; this is a conservative heuristic:
    ~4 chars/token for Latin, ~1.5 chars/token for CJK. Used only for
    warnings, never for hard truncation (char budget in §7.3 is authoritative).
    """
    if not text:
        return 0
    latin = sum(1 for c in text if ord(c) < 0x2E80)
    cjk = len(text) - latin
    return int(latin / 4 + cjk / 1.5)


def _max_prompt_tokens(model_id: str | None) -> int | None:
    # max_prompt_tokens lives under `constraints` (like max_prompt_chars),
    # not under `capabilities`.
    if not model_id:
        return None
    from app.models_lib import registry

    try:
        model = registry.get_model(model_id)
        constraints = model.get("constraints") or {}
        tokens = constraints.get("max_prompt_tokens")
        return int(tokens) if tokens else None
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _model_caps(model_id: str | None) -> dict:
    if not model_id:
        return {}
    from app.models_lib import registry

    try:
        model = registry.get_model(model_id)
    except Exception:
        return {}
    return model.get("capabilities") or {}


def _max_reference_images(model_id: str | None) -> int:
    caps = _model_caps(model_id)
    try:
        return int(caps.get("max_reference_images") or defaults.SHOT_MAX_REFERENCES)
    except (TypeError, ValueError):
        return defaults.SHOT_MAX_REFERENCES


def _max_prompt_chars(model_id: str | None) -> int:
    if model_id:
        from app.models_lib import registry

        try:
            model = registry.get_model(model_id)
            constraints = model.get("constraints") or {}
            if constraints.get("max_prompt_chars"):
                return int(constraints["max_prompt_chars"])
        except Exception:
            pass
    return defaults.PROMPT_MAX_CHARS


def _slot_order(model_id: str | None) -> list[str]:
    caps = _model_caps(model_id)
    order = caps.get("prompt_section_order")
    if isinstance(order, list) and order:
        known = [s for s in order if s in SLOT_ORDER]
        rest = [s for s in SLOT_ORDER if s not in known]
        return known + rest
    return list(SLOT_ORDER)
