"""Subtitle-driven edit analysis (P1-6, design report §8.5).

POST /subtitle-tracks/{track_id}/apply-to-timeline analyses how the track's
cues relate to a timeline's cut list and proposes per-clip duration
adjustments. It is analysis + proposal ONLY: the timeline is never rewritten.
The user confirms by PATCHing /timelines/{id} with adjusted clip durations
(the existing edit flow, §8.1 F-12).

Cue -> clip linkage: a cue's [start_sec, end_sec) window is matched against
the timeline's absolute clip windows (derived from timeline_clips positions
and their in/out ranges); the matched clip's shot_id links the cue to its shot.
Estimated speech duration = character count / CPS (cps is caller-tunable;
[가정] default 12.0 chars/sec).
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError


def analyze_cues_vs_timeline(
    db: Session,
    track_id: uuid.UUID,
    timeline_id: uuid.UUID,
    cps: float,
) -> dict:
    """Return {track_id, timeline_id, adjustments, warnings}; no DB writes."""
    from app.models.subtitle import SubtitleCue, SubtitleTrack
    from app.models.timeline import Timeline, TimelineClip

    track = db.get(SubtitleTrack, track_id)
    if track is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Subtitle track not found.")
    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    if track.project_id != timeline.project_id:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Subtitle track and timeline belong to different projects.",
        )

    # Absolute clip windows: accumulate (out - in) per position order.
    clips = (
        db.query(TimelineClip)
        .filter(TimelineClip.timeline_id == timeline_id)
        .order_by(TimelineClip.position)
        .all()
    )
    windows: list[tuple[TimelineClip, float, float, float]] = []
    cursor = 0.0
    for clip in clips:
        dur = clip.out_sec - clip.in_sec
        windows.append((clip, cursor, cursor + dur, dur))
        cursor += dur

    cues = (
        db.query(SubtitleCue)
        .filter(SubtitleCue.track_id == track_id)
        .order_by(SubtitleCue.start_sec)
        .all()
    )

    adjustments: list[dict] = []
    warnings: list[str] = []
    if not clips:
        warnings.append("Timeline has no clips; nothing to analyse.")
        return {
            "track_id": track_id,
            "timeline_id": timeline_id,
            "adjustments": adjustments,
            "warnings": warnings,
        }

    for cue in cues:
        text_len = len((cue.text or "").strip())
        suggested = round(text_len / cps, 2)
        match: TimelineClip | None = None
        best_overlap = 0.0
        for clip, start_t, end_t, _dur in windows:
            overlap = min(cue.end_sec, end_t) - max(cue.start_sec, start_t)
            if overlap > best_overlap:
                best_overlap = overlap
                match = clip
        if match is None:
            warnings.append(
                f"cue {cue.id} [{cue.start_sec:.2f}s-{cue.end_sec:.2f}s] matches no "
                "timeline clip; skipped."
            )
            continue
        current_dur = round(match.out_sec - match.in_sec, 2)
        fits = suggested <= current_dur
        if not fits:
            warnings.append(
                f"cue {cue.id}: speech ~{suggested}s exceeds clip "
                f"{match.id} ({current_dur}s, shot {match.shot_id}); "
                "consider extending the clip or trimming the line."
            )
        adjustments.append(
            {
                "cue_id": cue.id,
                "shot_id": match.shot_id,
                "cue_start_sec": cue.start_sec,
                "cue_end_sec": cue.end_sec,
                "cue_text_len": text_len,
                "current_dur_sec": current_dur,
                "suggested_dur_sec": suggested,
                "fits": fits,
            }
        )
    return {
        "track_id": track_id,
        "timeline_id": timeline_id,
        "adjustments": adjustments,
        "warnings": warnings,
    }
