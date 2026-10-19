"""Camera presets and the 180-degree rule check (P1-2).

Built-in presets are immutable from the API and upserted idempotently by
`ensure_builtin_camera_presets` (called from `python -m app.seed`).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models.camera import CameraPreset

CAMERA_SIDES = ("left", "right", "center")

# Six built-in presets. movement/angle use the contract enums
# (packages/contracts/enums.json: camera_movement, camera_angle).
BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "name": "정적 아이레벨",
        "description": "움직임 없는 눈높이 고정 샷. 대사·감정 연기에 집중할 때.",
        "lens": "35mm",
        "movement": "static",
        "angle": "eye_level",
        "settings": {"stabilization": "tripod"},
    },
    {
        "name": "슬로우 푸시인",
        "description": "주인공에게 천천히 다가가는 푸시인. 긴장감·몰입 상승.",
        "lens": "50mm",
        "movement": "dolly_in",
        "angle": "eye_level",
        "settings": {"speed": "slow"},
    },
    {
        "name": "핸드헬드 트래킹",
        "description": "인물을 따라가는 핸드헬드. 다큐·현장감 있는 장면에.",
        "lens": "24mm",
        "movement": "handheld",
        "angle": "eye_level",
        "settings": {"stabilization": "light"},
    },
    {
        "name": "크레인 상승",
        "description": "위로 치솟으며 공간을 여는 크레인 샷. 장면 전환·오프닝에.",
        "lens": "28mm",
        "movement": "crane",
        "angle": "high",
        "settings": {"direction": "up"},
    },
    {
        "name": "더치 앵글",
        "description": "기울어진 앵글. 불안·혼란·긴박한 심리에.",
        "lens": "35mm",
        "movement": "static",
        "angle": "dutch",
        "settings": {"roll_deg": 15},
    },
    {
        "name": "오버헤드 풀백",
        "description": "위에서 내려다보며 멀어지는 샷. 고립·스케일 강조.",
        "lens": "24mm",
        "movement": "dolly_out",
        "angle": "birds_eye",
        "settings": {"direction": "back"},
    },
]


def ensure_builtin_camera_presets(db: Session) -> int:
    """Upsert the built-in presets by name. Idempotent; returns rows touched."""
    from app.core.contracts import check_enum

    touched = 0
    for spec in BUILTIN_PRESETS:
        check_enum("camera_movement", spec["movement"])
        check_enum("camera_angle", spec["angle"])
        row = db.query(CameraPreset).filter(CameraPreset.name == spec["name"]).first()
        if row is None:
            db.add(CameraPreset(is_builtin=True, **spec))
        else:
            for key, value in spec.items():
                setattr(row, key, value)
            row.is_builtin = True
        touched += 1
    db.flush()
    return touched


def list_presets(db: Session) -> list[CameraPreset]:
    return db.query(CameraPreset).order_by(CameraPreset.name).all()


def get_preset(db: Session, preset_id: uuid.UUID) -> CameraPreset:
    row = db.get(CameraPreset, preset_id)
    if row is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Camera preset not found.")
    return row


def check_camera_axis(db: Session, scene_id: uuid.UUID) -> list[dict[str, Any]]:
    """180-degree rule: flag consecutive shots whose camera_side flips.

    Only shots with an explicit side participate; `center` is neutral and
    never a violation by itself. A flip means left<->right between two
    adjacent shots that both declare a side.
    """
    from app.models.production import Shot
    from app.services.scene import get_scene

    get_scene(db, scene_id)  # 404 when the scene does not exist
    shots = (
        db.query(Shot)
        .filter(Shot.scene_id == scene_id)
        .order_by(Shot.order_index)
        .all()
    )
    violations: list[dict[str, Any]] = []
    prev: Any = None
    for shot in shots:
        if prev is not None:
            a, b = prev.camera_side, shot.camera_side
            if a in ("left", "right") and b in ("left", "right") and a != b:
                violations.append(
                    {
                        "shot_id": shot.id,
                        "message": (
                            "180도 규칙 위반: 이전 샷(#"
                            f"{prev.order_index})과 카메라 축이 바뀌었습니다 "
                            f"({a} → {b}). 시청자가 방향을 혼동할 수 있습니다."
                        ),
                    }
                )
        prev = shot
    return violations
