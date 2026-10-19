"""Subtitle tracks, cues, styles, generation and export (F-13, design §4.13)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import episode as episode_schema
from app.schemas import subtitle as s
from app.services import subtitle_edit as subtitle_edit_service
from app.services import subtitles as subtitle_service

router = APIRouter(tags=["subtitles"])


def _out(track) -> s.SubtitleTrackOut:
    return s.SubtitleTrackOut(
        id=track.id, project_id=track.project_id,
        language_code=track.language_code, label=track.label,
        is_default=track.is_default, created_at=track.created_at,
    )


@router.get("/projects/{project_id}/subtitle-tracks", response_model=list[s.SubtitleTrackOut])
def list_tracks(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return [_out(t) for t in subtitle_service.list_tracks(db, project_id)]


@router.post("/projects/{project_id}/subtitle-tracks", response_model=s.SubtitleTrackOut,
             status_code=201)
def create_track(project_id: uuid.UUID, body: s.SubtitleTrackCreate,
                 db: Session = Depends(get_db)):
    return _out(subtitle_service.create_track(
        db, project_id,
        **body.model_dump(exclude={"is_default"})))  # first track auto-defaults


@router.delete("/projects/{project_id}/subtitle-tracks/{track_id}", status_code=204)
def delete_track(project_id: uuid.UUID, track_id: uuid.UUID,
                 db: Session = Depends(get_db)):
    subtitle_service.delete_track(db, track_id)
    return None


@router.get("/subtitle-tracks/{track_id}/cues", response_model=list[s.SubtitleCueOut])
def list_cues(track_id: uuid.UUID, db: Session = Depends(get_db)):
    return [
        s.SubtitleCueOut(id=c.id, track_id=c.track_id, start_sec=c.start_sec,
                         end_sec=c.end_sec, text=c.text,
                         style_overrides=c.style_overrides)
        for c in subtitle_service.get_cues(db, track_id)
    ]


@router.put("/subtitle-tracks/{track_id}/cues", response_model=list[s.SubtitleCueOut])
def replace_cues(track_id: uuid.UUID, body: s.SubtitleCueReplace,
                 db: Session = Depends(get_db)):
    cues = subtitle_service.save_cues(
        db, track_id, [c.model_dump() for c in body.cues]
    )
    return [
        s.SubtitleCueOut(id=c.id, track_id=c.track_id, start_sec=c.start_sec,
                         end_sec=c.end_sec, text=c.text,
                         style_overrides=c.style_overrides)
        for c in cues
    ]


@router.get("/subtitle-tracks/{track_id}/export")
def export_track(track_id: uuid.UUID,
                 format: str = Query(default="srt", pattern="^(srt|vtt)$"),
                 db: Session = Depends(get_db)):
    cues = subtitle_service.get_cues(db, track_id)
    body = subtitle_service.to_vtt(cues) if format == "vtt" else subtitle_service.to_srt(cues)
    media = "text/vtt" if format == "vtt" else "application/x-subrip"
    return PlainTextResponse(body, media_type=media)


@router.post("/subtitle-tracks/{track_id}/generate",
             response_model=list[s.SubtitleCueOut], status_code=201)
def generate_cues(track_id: uuid.UUID, db: Session = Depends(get_db)):
    """Seed the track with one cue per shot dialogue (heuristic starter)."""
    cues = subtitle_service.generate_cues_from_dialogue(db, track_id)
    return [
        s.SubtitleCueOut(id=c.id, track_id=c.track_id, start_sec=c.start_sec,
                         end_sec=c.end_sec, text=c.text,
                         style_overrides=c.style_overrides)
        for c in cues
    ]


@router.post("/subtitle-tracks/{track_id}/apply-to-timeline",
             response_model=episode_schema.SubtitleEditApplyResponse)
def apply_to_timeline(track_id: uuid.UUID,
                      body: episode_schema.SubtitleEditApplyRequest,
                      db: Session = Depends(get_db)):
    """P1-6 subtitle-driven edit analysis: cue text lengths -> per-clip
    duration proposals. Analysis only; the timeline is never rewritten."""
    result = subtitle_edit_service.analyze_cues_vs_timeline(
        db, track_id, body.timeline_id, body.cps)
    return episode_schema.SubtitleEditApplyResponse(
        track_id=result["track_id"],
        timeline_id=result["timeline_id"],
        adjustments=[
            episode_schema.SubtitleClipAdjustment(**a)
            for a in result["adjustments"]
        ],
        warnings=result["warnings"],
    )


@router.get("/projects/{project_id}/subtitle-style", response_model=s.SubtitleStyleOut)
def get_style(project_id: uuid.UUID,
              track_id: uuid.UUID | None = Query(default=None),
              db: Session = Depends(get_db)):
    return s.SubtitleStyleOut(**subtitle_service.get_style(db, project_id, track_id))


@router.put("/projects/{project_id}/subtitle-style", response_model=s.SubtitleStyleOut)
def put_style(project_id: uuid.UUID, body: s.SubtitleStyleIn,
              track_id: uuid.UUID | None = Query(default=None),
              db: Session = Depends(get_db)):
    return s.SubtitleStyleOut(**subtitle_service.save_style(
        db, project_id, body.model_dump(exclude_unset=True), track_id))
