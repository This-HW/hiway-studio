"""Character CRUD, references, wardrobe (design §4.7)."""
from __future__ import annotations

import shutil
import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.files import paths
from app.models.character import Character, CharacterReference, CharacterWardrobe
from app.models.production import ShotReference
from app.models.project import Project


def _character_or_404(db: Session, character_id: uuid.UUID) -> Character:
    character = db.get(Character, character_id)
    if character is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Character not found.")
    return character


def _char_dir(character: Character) -> str:
    if character.project_id is None:
        return f"library/characters/{character.id}"
    return f"projects/{character.project_id}/characters/{character.id}"


def create_character(
    db: Session,
    name: str,
    appearance_prompt: str,
    description: str | None,
    negative_prompt: str | None,
    consistency_method: str,
    default_strength: float,
    style_notes: str | None,
    tags: list[str],
    project_id: uuid.UUID | None,
) -> Character:
    check_enum("consistency_method", consistency_method)
    if project_id is not None and db.get(Project, project_id) is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    character = Character(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        role="supporting",
        description=description,
        appearance_prompt=appearance_prompt,
        negative_prompt=negative_prompt,
        consistency_method=consistency_method,
        default_strength=default_strength,
        style_notes=style_notes,
        tags=tags,
    )
    db.add(character)
    db.flush()
    # Character directory is created on first file write.
    return character


def list_characters(db: Session, project_id: uuid.UUID, scope: str = "all") -> list[Character]:
    """scope: project | global | all (default all)."""
    if scope == "project":
        q = db.query(Character).filter(Character.project_id == project_id)
    elif scope == "global":
        q = db.query(Character).filter(Character.project_id.is_(None))
    elif scope == "all":
        q = db.query(Character).filter(
            (Character.project_id == project_id) | (Character.project_id.is_(None))
        )
    else:
        raise StudioError(ErrorCode.VALIDATION_ERROR, f"Unknown scope: {scope}.")
    return q.order_by(Character.created_at).all()


def get_character(db: Session, character_id: uuid.UUID) -> Character:
    return _character_or_404(db, character_id)


_APPEARANCE_FIELDS = frozenset(
    {
        "appearance_prompt",
        "negative_prompt",
        "consistency_method",
        "default_strength",
        "style_notes",
    }
)


def update_character(db: Session, character_id: uuid.UUID, fields: dict) -> Character:
    character = _character_or_404(db, character_id)
    changed = False
    appearance_changed = False
    for key, value in fields.items():
        if value is None or not hasattr(character, key):
            continue
        if key == "role":
            check_enum("character_role", value)
        if key == "consistency_method":
            check_enum("consistency_method", value)
        if key in _APPEARANCE_FIELDS and getattr(character, key) != value:
            appearance_changed = True
        setattr(character, key, value)
        changed = True
    if changed:
        character.version += 1
    if appearance_changed:
        _mark_dependent_shots_stale(db, character_id)
    db.flush()
    return character


def _mark_dependent_shots_stale(db: Session, character_id: uuid.UUID) -> None:
    """Set frame_stale on shots that reference this character and have takes.

    Design §8.1: an appearance change invalidates previously generated
    frames of dependent shots (same rule as shot prompt edits, F-06).
    """
    from app.models.generation import Generation
    from app.models.production import Shot

    shot_ids = (
        db.query(ShotReference.shot_id)
        .filter(ShotReference.character_id == character_id)
        .distinct()
        .all()
    )
    if not shot_ids:
        return
    ids = [row[0] for row in shot_ids]
    stale_ids = (
        db.query(Shot.id)
        .join(Generation, Generation.shot_id == Shot.id)
        .filter(
            Shot.id.in_(ids),
            Generation.is_batch.is_(False),
        )
        .distinct()
        .all()
    )
    for (shot_id,) in stale_ids:
        shot = db.get(Shot, shot_id)
        if shot is not None:
            shot.frame_stale = True


def delete_character(db: Session, character_id: uuid.UUID) -> None:
    character = _character_or_404(db, character_id)
    in_use = (
        db.query(ShotReference)
        .filter(ShotReference.character_id == character_id)
        .count()
    )
    if in_use:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Character is referenced by {in_use} shot(s); archive it instead.",
        )
    db.delete(character)
    db.flush()
    char_dir = paths.resolve(_char_dir(character))
    if char_dir.exists():
        shutil.rmtree(char_dir)


def archive_character(db: Session, character_id: uuid.UUID) -> Character:
    character = _character_or_404(db, character_id)
    character.is_archived = True
    db.flush()
    return character


def add_reference(
    db: Session,
    character_id: uuid.UUID,
    image_path: str,
    role: str,
    is_primary: bool,
    source: str,
) -> CharacterReference:
    character = _character_or_404(db, character_id)
    check_enum("reference_role", role)
    check_enum("reference_source", source)
    src = paths.resolve(image_path)
    if not src.exists():
        raise StudioError(ErrorCode.NOT_FOUND, "Source image not found.")
    dst_rel = f"{_char_dir(character)}/references/{role}_{uuid.uuid4().hex[:8]}.png"
    dst = paths.ensure_parent(paths.projects_root() / dst_rel)
    shutil.copy2(src, dst)
    if is_primary:
        for ref in db.query(CharacterReference).filter_by(character_id=character_id).all():
            ref.is_primary = False
    ref = CharacterReference(
        id=uuid.uuid4(),
        character_id=character_id,
        image_path=paths.assert_relative(dst_rel),
        role=role,
        is_primary=is_primary,
        source=source,
    )
    db.add(ref)
    db.flush()
    return ref


def list_references(db: Session, character_id: uuid.UUID) -> list[CharacterReference]:
    _character_or_404(db, character_id)
    return (
        db.query(CharacterReference)
        .filter(CharacterReference.character_id == character_id)
        .order_by(CharacterReference.created_at)
        .all()
    )


def update_reference(
    db: Session, reference_id: uuid.UUID, role: str | None, is_primary: bool | None
) -> CharacterReference:
    ref = db.get(CharacterReference, reference_id)
    if ref is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Reference not found.")
    if role is not None:
        check_enum("reference_role", role)
        ref.role = role
    if is_primary is not None:
        if is_primary:
            for other in (
                db.query(CharacterReference)
                .filter(CharacterReference.character_id == ref.character_id)
                .all()
            ):
                other.is_primary = False
        ref.is_primary = is_primary
    db.flush()
    return ref


def delete_reference(db: Session, reference_id: uuid.UUID) -> None:
    ref = db.get(CharacterReference, reference_id)
    if ref is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Reference not found.")
    db.delete(ref)
    db.flush()
    # Reference uploads are user-managed files (not immutable job artifacts).
    target = paths.resolve(ref.image_path)
    if target.exists():
        target.unlink(missing_ok=True)


def create_wardrobe(
    db: Session,
    character_id: uuid.UUID,
    name: str,
    prompt_fragment: str,
    reference_image_id: uuid.UUID | None,
    is_default: bool,
) -> CharacterWardrobe:
    _character_or_404(db, character_id)
    if reference_image_id is not None:
        ref = db.get(CharacterReference, reference_image_id)
        if ref is None or ref.character_id != character_id:
            raise StudioError(ErrorCode.NOT_FOUND, "Reference image not found for this character.")
    if is_default:
        for w in db.query(CharacterWardrobe).filter_by(character_id=character_id).all():
            w.is_default = False
    wardrobe = CharacterWardrobe(
        id=uuid.uuid4(),
        character_id=character_id,
        name=name,
        prompt_fragment=prompt_fragment,
        reference_image_id=reference_image_id,
        is_default=is_default,
    )
    db.add(wardrobe)
    db.flush()
    return wardrobe


def list_wardrobe(db: Session, character_id: uuid.UUID) -> list[CharacterWardrobe]:
    _character_or_404(db, character_id)
    return (
        db.query(CharacterWardrobe)
        .filter(CharacterWardrobe.character_id == character_id)
        .order_by(CharacterWardrobe.created_at)
        .all()
    )


def get_master_thumbnail(db: Session, character_id: uuid.UUID) -> str | None:
    """Relative thumbnail path of the primary master reference, if any.

    Falls back to the master image itself when no thumbnail file exists
    (older rows). Served to the web via GET /files/{rel_path}.
    """
    from pathlib import PurePosixPath

    from app.files import paths

    ref = (
        db.query(CharacterReference)
        .filter(
            CharacterReference.character_id == character_id,
            CharacterReference.role == "master",
            CharacterReference.is_primary.is_(True),
        )
        .order_by(CharacterReference.created_at.desc())
        .first()
    )
    if ref is None:
        return None
    rel = PurePosixPath(ref.image_path)
    thumb_rel = str(rel.parent / f"{rel.stem}.thumb.jpg")
    if (paths.projects_root() / thumb_rel).is_file():
        return thumb_rel
    return ref.image_path
