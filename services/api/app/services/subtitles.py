"""Subtitle tracks, cues and styles (F-13, P0-15, design §4.16).

The cue text (SRT/VTT-equivalent) is the canonical truth (G-09); burn-in
happens only at render time (D-14) via media/subtitle_render.py.

Validation (service layer, so both router and task share it):
- start_sec > 0 and end_sec > start_sec, else 422 VALIDATION_ERROR.
- overlapping cues inside one track -> 422 SUBTITLE_OVERLAP (§4.16).
- cue duration outside 1..7s is NOT rejected here; it is a QC `warn`
  (subtitle_timing, design §10.1) — the user may intentionally keep it.

Every cue/style/mode/track change bumps the affected timelines'
content_version so QC reports go stale (design §10.4).

Render lock (F13): a cue/style/track change that affects a timeline while it
is `rendering` raises 409 RENDER_LOCKED (same rule as clip edits) before
anything is written; `timelines.bump_content_version` enforces it again
atomically against a concurrent render claim.

Track membership (B4B-H3): which timelines burn a track can change
concurrently (a timeline's subtitle_track_ids / subtitle_mode). Cue, style
and track-delete edits and those timeline selection changes therefore take
one common lock first — the project row (`lock_subtitle_scope`) — and read
the selecting timelines only after it, in the same transaction. A selection
change that lands first is seen (its timeline is bumped / RENDER_LOCKED);
one that comes later waits for the edit's commit. Lock order: project ->
timeline (ADR-0013).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError


def _get_project(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def _get_track(db: Session, track_id: uuid.UUID):
    from app.models.subtitle import SubtitleTrack

    track = db.get(SubtitleTrack, track_id)
    if track is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Subtitle track not found.")
    return track


def lock_subtitle_scope(db: Session, project_id: uuid.UUID) -> None:
    """THE common lock of subtitle edits and timeline track selection
    (B4B-H3): row-lock the project (SELECT ... FOR NO KEY UPDATE; SQLite
    serializes writers). Take it before reading which timelines select a
    track and before writing any timeline row (lock order project ->
    timeline)."""
    from app.models.project import Project
    from app.services.generations import lock_row

    if lock_row(db, Project, project_id) is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")


def _locked_track(db: Session, track_id: uuid.UUID):
    """Lock the track's project, then re-read the track (it may have been
    deleted by an edit that held the lock before us)."""
    from app.models.subtitle import SubtitleTrack

    track = _get_track(db, track_id)
    lock_subtitle_scope(db, track.project_id)
    track = db.get(SubtitleTrack, track_id, populate_existing=True)
    if track is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Subtitle track not found.")
    return track


def _timelines_for_track(db: Session, track) -> list:
    """Timelines that burn this track (current DB state: callers hold the
    subtitle scope lock, so the membership cannot change until commit)."""
    from app.models.timeline import Timeline

    timelines = (
        db.query(Timeline)
        .filter(Timeline.project_id == track.project_id)
        .populate_existing()
        .all()
    )
    wanted = str(track.id)
    return [
        tl for tl in timelines
        if wanted in [str(x) for x in (tl.subtitle_track_ids or [])]
    ]


def _require_track_editable(db: Session, track) -> None:
    """409 RENDER_LOCKED when a timeline burning this track is rendering."""
    from app.services.render import _require_editable

    for tl in _timelines_for_track(db, track):
        _require_editable(tl)


def _require_project_editable(db: Session, project_id: uuid.UUID) -> None:
    """409 RENDER_LOCKED when any timeline of the project is rendering
    (a project-default style change bumps every timeline)."""
    from app.models.timeline import Timeline
    from app.services.render import _require_editable

    for tl in (
        db.query(Timeline)
        .filter(Timeline.project_id == project_id)
        .populate_existing()
        .all()
    ):
        _require_editable(tl)


def _bump_timelines_for_track(db: Session, track) -> None:
    """Bump content_version of timelines that burn this track."""
    from app.services.timelines import bump_content_version

    for tl in _timelines_for_track(db, track):
        bump_content_version(db, tl.id)


def _validate_cues(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate ranges and overlap. Returns cues sorted by start_sec."""
    cleaned: list[dict[str, Any]] = []
    for i, cue in enumerate(cues):
        try:
            start = float(cue["start_sec"])
            end = float(cue["end_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Cue #{i} needs numeric start_sec/end_sec.",
                details={"cue_index": i},
            ) from exc
        text = str(cue.get("text") or "")
        if not (start > 0):
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Cue #{i}: start_sec must be > 0.",
                details={"cue_index": i, "start_sec": start},
            )
        if not (end > start):
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Cue #{i}: end_sec must be > start_sec.",
                details={"cue_index": i, "start_sec": start, "end_sec": end},
            )
        cleaned.append(
            {
                "start_sec": start,
                "end_sec": end,
                "text": text,
                "word_timings": cue.get("word_timings"),
                "style_overrides": cue.get("style_overrides"),
            }
        )
    cleaned.sort(key=lambda c: c["start_sec"])
    for prev, cur in zip(cleaned, cleaned[1:]):
        if cur["start_sec"] < prev["end_sec"]:
            raise StudioError(
                ErrorCode.SUBTITLE_OVERLAP,
                "Subtitle cues overlap.",
                details={
                    "prev": {"start_sec": prev["start_sec"], "end_sec": prev["end_sec"]},
                    "cur": {"start_sec": cur["start_sec"], "end_sec": cur["end_sec"]},
                },
            )
    return cleaned


def list_tracks(db: Session, project_id: uuid.UUID) -> list:
    from app.models.subtitle import SubtitleTrack

    _get_project(db, project_id)
    return (
        db.query(SubtitleTrack)
        .filter(SubtitleTrack.project_id == project_id)
        .order_by(SubtitleTrack.display_order, SubtitleTrack.created_at)
        .all()
    )


def create_track(
    db: Session,
    project_id: uuid.UUID,
    *,
    language_code: str,
    label: str | None = None,
) -> Any:
    from app.models.subtitle import SubtitleTrack

    _get_project(db, project_id)
    code = (language_code or "").strip().lower()
    if not code:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "language_code is required.")
    existing = list_tracks(db, project_id)
    track = SubtitleTrack(
        project_id=project_id,
        language_code=code,
        label=(label or "").strip() or None,
        display_order=len(existing),
        is_default=not existing,  # first track becomes the default
    )
    db.add(track)
    db.flush()
    return track


def delete_track(db: Session, track_id: uuid.UUID) -> None:
    from app.models.subtitle import SubtitleTrack

    track = _locked_track(db, track_id)
    _require_track_editable(db, track)
    _bump_timelines_for_track(db, track)
    db.delete(track)  # cues + track style cascade
    db.flush()


def get_cues(db: Session, track_id: uuid.UUID) -> list:
    from app.models.subtitle import SubtitleCue

    _get_track(db, track_id)
    return (
        db.query(SubtitleCue)
        .filter(SubtitleCue.track_id == track_id)
        .order_by(SubtitleCue.start_sec)
        .all()
    )


def save_cues(db: Session, track_id: uuid.UUID, cues: list[dict[str, Any]]) -> list:
    """Atomically replace a track's cues (validated)."""
    from app.models.subtitle import SubtitleCue

    track = _locked_track(db, track_id)
    _require_track_editable(db, track)
    cleaned = _validate_cues(cues)
    db.query(SubtitleCue).filter(SubtitleCue.track_id == track_id).delete()
    rows = [
        SubtitleCue(
            track_id=track_id,
            start_sec=c["start_sec"],
            end_sec=c["end_sec"],
            text=c["text"],
            word_timings=c["word_timings"],
            style_overrides=c["style_overrides"],
        )
        for c in cleaned
    ]
    db.add_all(rows)
    _bump_timelines_for_track(db, track)
    db.flush()
    return rows


def get_style(db: Session, project_id: uuid.UUID, track_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Effective style: track override wins, else the project default."""
    from app.models.subtitle import SubtitleStyle

    _get_project(db, project_id)
    style = None
    if track_id is not None:
        _get_track(db, track_id)
        style = (
            db.query(SubtitleStyle)
            .filter(SubtitleStyle.track_id == track_id)
            .order_by(SubtitleStyle.created_at.desc())
            .first()
        )
    if style is None:
        style = (
            db.query(SubtitleStyle)
            .filter(
                SubtitleStyle.project_id == project_id,
                SubtitleStyle.track_id.is_(None),
            )
            .order_by(SubtitleStyle.created_at.desc())
            .first()
        )
    if style is None:
        # Unsaved defaults (design §4.16) — returned, not persisted.
        return {
            "font_family": "Noto Sans KR",
            "font_size_px": 48,
            "color": "#FFFFFF",
            "position": "bottom",
            "background_color": None,
            "background_opacity": 0.6,
            "line_break_rule": "auto",
            "safe_area_margin_pct": 5.0,
        }
    return {
        "font_family": style.font_family,
        "font_size_px": style.font_size_px,
        "color": style.color,
        "position": style.position,
        "background_color": style.background_color,
        "background_opacity": style.background_opacity,
        "line_break_rule": style.line_break_rule,
        "safe_area_margin_pct": style.safe_area_margin_pct,
    }


def save_style(
    db: Session,
    project_id: uuid.UUID,
    style: dict[str, Any],
    track_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Upsert the project default style (track_id None) or a track override."""
    from app.models.subtitle import SubtitleStyle

    _get_project(db, project_id)
    if track_id is not None:
        track = _get_track(db, track_id)
        if track.project_id != project_id:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR, "Track does not belong to this project."
            )
        track = _locked_track(db, track_id)
        _require_track_editable(db, track)
    else:
        lock_subtitle_scope(db, project_id)
        _require_project_editable(db, project_id)
    position = style.get("position", "bottom")
    check_enum("subtitle_position", position)
    row = (
        db.query(SubtitleStyle)
        .filter(
            SubtitleStyle.project_id == project_id,
            (SubtitleStyle.track_id == track_id)
            if track_id is not None
            else SubtitleStyle.track_id.is_(None),
        )
        .order_by(SubtitleStyle.created_at.desc())
        .first()
    )
    fields = {
        "font_family": str(style.get("font_family") or "Noto Sans KR"),
        "font_size_px": int(style.get("font_size_px") or 48),
        "color": str(style.get("color") or "#FFFFFF"),
        "position": position,
        "background_color": style.get("background_color"),
        "background_opacity": float(style.get("background_opacity", 0.6)),
        "line_break_rule": str(style.get("line_break_rule") or "auto"),
        "safe_area_margin_pct": float(style.get("safe_area_margin_pct", 5.0)),
    }
    if not (0.0 <= fields["background_opacity"] <= 1.0):
        raise StudioError(
            ErrorCode.VALIDATION_ERROR, "background_opacity must be 0..1."
        )
    if row is None:
        row = SubtitleStyle(project_id=project_id, track_id=track_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    if track_id is not None:
        _bump_timelines_for_track(db, _get_track(db, track_id))
    else:
        # Project-default style change affects every timeline of the project.
        from app.models.timeline import Timeline
        from app.services.timelines import bump_content_version

        for tl in (
            db.query(Timeline)
            .filter(Timeline.project_id == project_id)
            .populate_existing()
            .all()
        ):
            bump_content_version(db, tl.id)
    db.flush()
    return fields


def _fmt_ts(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ts_vtt(seconds: float) -> str:
    return _fmt_ts(seconds).replace(",", ".")


def to_srt(cues: list) -> str:
    lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_ts(cue.start_sec)} --> {_fmt_ts(cue.end_sec)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def to_vtt(cues: list) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{_fmt_ts_vtt(cue.start_sec)} --> {_fmt_ts_vtt(cue.end_sec)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def generate_cues_from_dialogue(db: Session, track_id: uuid.UUID) -> list:
    """Fill a track with one cue per shot dialogue (heuristic starter).

    Each dialogue becomes a 2.5s cue laid end to end from t=0.5s — a valid
    starting point the user edits. Empty dialogues are skipped.
    """
    from app.models.production import Scene, Shot

    track = _get_track(db, track_id)
    shots = (
        db.query(Shot)
        .join(Scene, Scene.id == Shot.scene_id)
        .filter(Scene.project_id == track.project_id)
        .order_by(Scene.order_index, Shot.order_index)
        .all()
    )
    cues: list[dict[str, Any]] = []
    cursor = 0.5
    for shot in shots:
        text = (shot.dialogue or "").strip()
        if not text:
            continue
        cues.append({"start_sec": cursor, "end_sec": cursor + 2.5, "text": text})
        cursor += 3.0  # 0.5s gap between cues
    return save_cues(db, track_id, cues)
