"""Camera keyframe CRUD + interpolated camera path (P2 3D previz, ADR-0010).

Coordinate system: meters, Y-up (documented on the model and schemas).

All operations are free — no jobs, no budget guard. Path interpolation is
plain linear interpolation between consecutive keyframes; this is a
camera-move planning aid, not a renderer.
"""
from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models.camera_previz import CameraKeyframe

_MAX_SAMPLES = 200


def _shot_or_404(db: Session, shot_id: uuid.UUID):
    from app.models.production import Shot

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Shot not found.")
    return shot


def _get_keyframe(db: Session, kf_id: uuid.UUID) -> CameraKeyframe:
    kf = db.get(CameraKeyframe, kf_id)
    if kf is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Camera keyframe not found.")
    return kf


def _check_duplicate_t_sec(db: Session, shot_id: uuid.UUID, t_sec: float,
                           exclude_id: uuid.UUID | None = None) -> None:
    q = db.query(CameraKeyframe).filter(
        CameraKeyframe.shot_id == shot_id,
        CameraKeyframe.t_sec == t_sec,
    )
    if exclude_id is not None:
        q = q.filter(CameraKeyframe.id != exclude_id)
    if q.first() is not None:
        raise StudioError(
            ErrorCode.DUPLICATE_KEYFRAME,
            f"A keyframe at t_sec={t_sec} already exists for this shot.",
        )


def list_keyframes(db: Session, shot_id: uuid.UUID) -> list[CameraKeyframe]:
    _shot_or_404(db, shot_id)
    return (
        db.query(CameraKeyframe)
        .filter(CameraKeyframe.shot_id == shot_id)
        .order_by(CameraKeyframe.t_sec)
        .all()
    )


def create_keyframe(db: Session, shot_id: uuid.UUID, *, t_sec: float,
                    position: dict, target: dict, fov_deg: float) -> CameraKeyframe:
    _shot_or_404(db, shot_id)
    if t_sec < 0:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "t_sec must be >= 0.")
    _check_duplicate_t_sec(db, shot_id, t_sec)
    kf = CameraKeyframe(
        shot_id=shot_id, t_sec=t_sec,
        position=position, target=target, fov_deg=fov_deg,
    )
    db.add(kf)
    try:
        db.flush()
    except IntegrityError:
        # Belt and suspenders: UNIQUE(shot_id, t_sec) raced with the check.
        db.rollback()
        raise StudioError(
            ErrorCode.DUPLICATE_KEYFRAME,
            f"A keyframe at t_sec={t_sec} already exists for this shot.",
        )
    return kf


def update_keyframe(db: Session, kf_id: uuid.UUID, *,
                    t_sec: float | None = None,
                    position: dict | None = None,
                    target: dict | None = None,
                    fov_deg: float | None = None) -> CameraKeyframe:
    kf = _get_keyframe(db, kf_id)
    if t_sec is not None:
        if t_sec < 0:
            raise StudioError(ErrorCode.VALIDATION_ERROR, "t_sec must be >= 0.")
        if t_sec != kf.t_sec:
            _check_duplicate_t_sec(db, kf.shot_id, t_sec, exclude_id=kf.id)
        kf.t_sec = t_sec
    if position is not None:
        kf.position = position
    if target is not None:
        kf.target = target
    if fov_deg is not None:
        kf.fov_deg = fov_deg
    db.flush()
    return kf


def delete_keyframe(db: Session, kf_id: uuid.UUID) -> None:
    kf = _get_keyframe(db, kf_id)
    db.delete(kf)
    db.flush()


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def camera_path(db: Session, shot_id: uuid.UUID, samples: int = 50) -> dict:
    """Interpolated camera path, keyframes sorted by t_sec.

    Linear interpolation per axis between consecutive keyframes. Returns
    evenly spaced samples across [first.t_sec, last.t_sec].
    """
    _shot_or_404(db, shot_id)
    kfs = list_keyframes(db, shot_id)
    n = max(2, min(int(samples), _MAX_SAMPLES))

    if not kfs:
        return {"shot_id": shot_id, "keyframe_count": 0, "samples": []}
    if len(kfs) == 1:
        kf = kfs[0]
        sample = {
            "t_sec": kf.t_sec, "position": kf.position,
            "target": kf.target, "fov_deg": kf.fov_deg,
        }
        return {"shot_id": shot_id, "keyframe_count": 1, "samples": [sample]}

    t0, t1 = kfs[0].t_sec, kfs[-1].t_sec
    out: list[dict] = []
    for i in range(n):
        t = t0 if n == 1 else t0 + (t1 - t0) * i / (n - 1)
        # Find the segment containing t.
        lo = kfs[0]
        hi = kfs[-1]
        for a, b in zip(kfs, kfs[1:]):
            if a.t_sec <= t <= b.t_sec:
                lo, hi = a, b
                break
        span = hi.t_sec - lo.t_sec
        f = 0.0 if span <= 0 else (t - lo.t_sec) / span
        out.append({
            "t_sec": t,
            "position": {
                "x": _lerp(lo.position["x"], hi.position["x"], f),
                "y": _lerp(lo.position["y"], hi.position["y"], f),
                "z": _lerp(lo.position["z"], hi.position["z"], f),
            },
            "target": {
                "x": _lerp(lo.target["x"], hi.target["x"], f),
                "y": _lerp(lo.target["y"], hi.target["y"], f),
                "z": _lerp(lo.target["z"], hi.target["z"], f),
            },
            "fov_deg": _lerp(lo.fov_deg, hi.fov_deg, f),
        })
    return {"shot_id": shot_id, "keyframe_count": len(kfs), "samples": out}
