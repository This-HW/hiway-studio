"""Deep-copy semantics (D-23).

clone_episode: copies an episode + its scenes, shots, and shot references.
Copied shots reset to status=draft with no approved generation — they
regenerate from draft.
duplicate_project: full project copy used by POST /projects/{id}/duplicate.
Copies DB rows (new ids everywhere) and the file tree. Generations, videos and
audio are never copied — copied shots regenerate from draft.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.files import paths
from app.models.asset import Asset
from app.models.character import Character, CharacterReference, CharacterWardrobe
from app.models.production import Scene, Shot, ShotReference
from app.models.project import Project
from app.models.story import Beat, Episode, Story
from app.models.style import StyleKit


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


def _copy_scenes(
    db: Session,
    src_project_id: uuid.UUID,
    src_episode_id: uuid.UUID | None,
    dst_project_id: uuid.UUID,
    dst_episode_id: uuid.UUID | None,
    beat_map: dict[uuid.UUID, uuid.UUID] | None = None,
    char_map: dict[uuid.UUID, uuid.UUID] | None = None,
    asset_map: dict[uuid.UUID, uuid.UUID] | None = None,
    ref_detail_map: dict[uuid.UUID, uuid.UUID] | None = None,
    kit_map: dict[uuid.UUID, uuid.UUID] | None = None,
) -> None:
    """Deep-copy scenes of one episode with their shots and shot references.

    New ids everywhere. Copied shots: status=draft, no approved generation,
    no stale flags. References are remapped through the given maps when the
    target was copied (duplicate_project), otherwise kept as-is
    (clone_episode — same project, targets unchanged).
    """
    beat_map = beat_map or {}
    char_map = char_map or {}
    asset_map = asset_map or {}
    ref_detail_map = ref_detail_map or {}
    kit_map = kit_map or {}

    shot_map: dict[uuid.UUID, uuid.UUID] = {}
    pending_continuity: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    episode_filter = (
        Scene.episode_id == src_episode_id
        if src_episode_id is not None
        else Scene.episode_id.is_(None)
    )
    scenes = (
        db.query(Scene)
        .filter(Scene.project_id == src_project_id, episode_filter)
        .order_by(Scene.order_index)
        .all()
    )
    for scene in scenes:
        new_scene_id = _new_id()
        db.add(
            Scene(
                id=new_scene_id,
                project_id=dst_project_id,
                episode_id=dst_episode_id,
                beat_id=beat_map.get(scene.beat_id, scene.beat_id),
                order_index=scene.order_index,
                title=scene.title,
                location=scene.location,
                time_of_day=scene.time_of_day,
                mood=scene.mood,
                lighting=scene.lighting,
                style_kit_id=kit_map.get(scene.style_kit_id, scene.style_kit_id),
            )
        )
        shots = (
            db.query(Shot)
            .filter(Shot.scene_id == scene.id)
            .order_by(Shot.order_index)
            .all()
        )
        for shot in shots:
            new_shot_id = _new_id()
            shot_map[shot.id] = new_shot_id
            db.add(
                Shot(
                    id=new_shot_id,
                    scene_id=new_scene_id,
                    order_index=shot.order_index,
                    shot_type=shot.shot_type,
                    camera_angle=shot.camera_angle,
                    movement=shot.movement,
                    duration_sec=shot.duration_sec,
                    action=shot.action,
                    dialogue=shot.dialogue,
                    emotion_weights=dict(shot.emotion_weights or {}),
                    sfx_notes=shot.sfx_notes,
                    intended_text=shot.intended_text,
                    status="draft",
                    approved_frame_generation_id=None,
                    approved_at=None,
                    frame_stale=False,
                    continuity_source_shot_id=None,  # remapped below
                    style_kit_id=kit_map.get(shot.style_kit_id, shot.style_kit_id),
                    coverage_source_frame_id=None,
                )
            )
            pending_continuity.append((new_shot_id, shot.continuity_source_shot_id))
            for ref in db.query(ShotReference).filter_by(shot_id=shot.id).all():
                db.add(
                    ShotReference(
                        id=_new_id(),
                        shot_id=new_shot_id,
                        character_id=char_map.get(ref.character_id, ref.character_id)
                        if ref.character_id
                        else None,
                        asset_id=asset_map.get(ref.asset_id, ref.asset_id)
                        if ref.asset_id
                        else None,
                        purpose=ref.purpose,
                        reference_detail_id=ref_detail_map.get(
                            ref.reference_detail_id, ref.reference_detail_id
                        ),
                    )
                )
    db.flush()

    # Continuity chains: point at the copied shot when the source was copied
    # too, otherwise keep the original (cross-episode) pointer.
    for new_shot_id, src_continuity_id in pending_continuity:
        target = shot_map.get(src_continuity_id, src_continuity_id)
        if target is not None:
            db.get(Shot, new_shot_id).continuity_source_shot_id = target
    db.flush()


def clone_episode(db: Session, episode_id: uuid.UUID) -> Episode:
    """Deep-copy one episode: row + scenes + shots + shot references."""
    src = db.get(Episode, episode_id)
    if src is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Episode not found.")
    clone = Episode(
        id=_new_id(),
        project_id=src.project_id,
        order_index=src.order_index,
        title=(src.title or "Untitled episode") + " (copy)",
        is_template=src.is_template,
        template_source_episode_id=src.id,
        status="draft",
    )
    db.add(clone)
    db.flush()
    _copy_scenes(db, src.project_id, src.id, src.project_id, clone.id)
    return clone


def _copy_characters(
    db: Session, src_project_id: uuid.UUID, dst_project_id: uuid.UUID
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    """Copy project-scoped characters (rows + files). Global ones are shared.

    Returns (character id map, character-reference id map) for remapping
    shot references in duplicate_project.
    """
    char_map: dict[uuid.UUID, uuid.UUID] = {}
    ref_map: dict[uuid.UUID, uuid.UUID] = {}
    chars = (
        db.query(Character)
        .filter(Character.project_id == src_project_id)
        .all()
    )
    for ch in chars:
        new_id = _new_id()
        char_map[ch.id] = new_id
        for ref in db.query(CharacterReference).filter_by(character_id=ch.id).all():
            new_ref_id = _new_id()
            ref_map[ref.id] = new_ref_id
            new_path = ref.image_path.replace(str(ch.id), str(new_id))
            db.add(
                CharacterReference(
                    id=new_ref_id,
                    character_id=new_id,
                    image_path=new_path,
                    role=ref.role,
                    is_primary=ref.is_primary,
                    source=ref.source,
                )
            )
        for w in db.query(CharacterWardrobe).filter_by(character_id=ch.id).all():
            db.add(
                CharacterWardrobe(
                    id=_new_id(),
                    character_id=new_id,
                    name=w.name,
                    prompt_fragment=w.prompt_fragment,
                    reference_image_id=ref_map.get(w.reference_image_id)
                    if w.reference_image_id
                    else None,
                    is_default=w.is_default,
                )
            )
        db.add(
            Character(
                id=new_id,
                project_id=dst_project_id,
                name=ch.name,
                role=ch.role,
                description=ch.description,
                appearance_prompt=ch.appearance_prompt,
                negative_prompt=ch.negative_prompt,
                consistency_method=ch.consistency_method,
                seed=ch.seed,
                default_strength=ch.default_strength,
                style_notes=ch.style_notes,
                tags=list(ch.tags or []),
                version=ch.version,
            )
        )
        src_dir = paths.project_dir(str(src_project_id)) / "characters" / str(ch.id)
        dst_dir = paths.project_dir(str(dst_project_id)) / "characters" / str(new_id)
        if src_dir.exists():
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
    db.flush()
    return char_map, ref_map


def duplicate_project(db: Session, project_id: uuid.UUID) -> Project:
    """Deep copy of a project: rows get new ids, files are copied.

    Copies: story+beats, project-scoped characters (+references, wardrobe, files),
    project-scoped style kits, project-scoped assets (+files), episodes.
    Global characters/assets/kits are shared, not copied.
    """
    src = db.get(Project, project_id)
    if src is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")

    dst = Project(
        id=_new_id(),
        title=f"{src.title} (copy)",
        aspect_ratio=src.aspect_ratio,
        default_style=src.default_style,
        default_models=dict(src.default_models or {}),
        category_pack_code=src.category_pack_code,
        monthly_budget_usd=src.monthly_budget_usd,
        is_archived=False,
    )
    db.add(dst)
    db.flush()

    # Project directory.
    dst_dir = paths.project_dir(str(dst.id))
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Style kits (project-scoped) — copied before characters so ids remap.
    kit_map: dict[uuid.UUID, uuid.UUID] = {}
    for kit in db.query(StyleKit).filter(StyleKit.project_id == src.id).all():
        new_kit_id = _new_id()
        kit_map[kit.id] = new_kit_id
        db.add(
            StyleKit(
                id=new_kit_id,
                project_id=dst.id,
                name=kit.name,
                version=kit.version,
                is_locked=kit.is_locked,
                color=kit.color,
                lighting=kit.lighting,
                texture=kit.texture,
                composition=kit.composition,
                lens=kit.lens,
                motion=kit.motion,
                editing_rhythm=kit.editing_rhythm,
                sound=kit.sound,
            )
        )
    if src.style_kit_id is not None and src.style_kit_id in kit_map:
        dst.style_kit_id = kit_map[src.style_kit_id]
    db.flush()

    # Story + beats.
    story = db.query(Story).filter(Story.project_id == src.id).one_or_none()
    beat_map: dict[uuid.UUID, uuid.UUID] = {}
    if story is not None:
        new_story = Story(
            id=_new_id(), project_id=dst.id, logline=story.logline, synopsis=story.synopsis
        )
        db.add(new_story)
        db.flush()
        for beat in (
            db.query(Beat).filter(Beat.story_id == story.id).order_by(Beat.order_index).all()
        ):
            new_beat_id = _new_id()
            beat_map[beat.id] = new_beat_id
            db.add(
                Beat(
                    id=new_beat_id,
                    story_id=new_story.id,
                    order_index=beat.order_index,
                    act=beat.act,
                    title=beat.title,
                    summary=beat.summary,
                )
            )
        db.flush()

    # Characters (rows + files) — returns id maps for shot-reference remapping.
    char_map, ref_detail_map = _copy_characters(db, src.id, dst.id)

    # Assets (project-scoped; rows + files).
    asset_map: dict[uuid.UUID, uuid.UUID] = {}
    for asset in db.query(Asset).filter(Asset.project_id == src.id).all():
        new_asset_id = _new_id()
        asset_map[asset.id] = new_asset_id
        old_name = Path(asset.image_path).name
        new_rel = f"projects/{dst.id}/assets/{new_asset_id}{Path(old_name).suffix}"
        db.add(
            Asset(
                id=new_asset_id,
                project_id=dst.id,
                kind=asset.kind,
                name=asset.name,
                image_path=new_rel,
                prompt_fragment=asset.prompt_fragment,
                tags=list(asset.tags or []),
            )
        )
        src_file = paths.resolve(asset.image_path)
        dst_file = paths.resolve(new_rel)
        if src_file.exists():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
    db.flush()

    # Episodes + their scenes/shots/references (deep copy).
    for ep in db.query(Episode).filter(Episode.project_id == src.id).all():
        new_ep_id = _new_id()
        db.add(
            Episode(
                id=new_ep_id,
                project_id=dst.id,
                order_index=ep.order_index,
                title=ep.title,
                is_template=ep.is_template,
                template_source_episode_id=ep.id,
                status="draft",
            )
        )
        db.flush()
        _copy_scenes(
            db,
            src.id,
            ep.id,
            dst.id,
            new_ep_id,
            beat_map=beat_map,
            char_map=char_map,
            asset_map=asset_map,
            ref_detail_map=ref_detail_map,
            kit_map=kit_map,
        )

    # Scenes not attached to any episode (e.g. from script import) also copy.
    _copy_scenes(
        db,
        src.id,
        None,
        dst.id,
        None,
        beat_map=beat_map,
        char_map=char_map,
        asset_map=asset_map,
        ref_detail_map=ref_detail_map,
        kit_map=kit_map,
    )

    # project.json snapshot for restore (§11.1).
    (dst_dir / "project.json").write_text(
        json.dumps(
            {"id": str(dst.id), "title": dst.title, "aspect_ratio": dst.aspect_ratio},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dst
