"""Audio upload registration + timeline placement (design §4.14).

Plus Phase-5 audio (P1-5): TTS dialogue (POST /shots/{id}/dialogue),
voice clone (POST /characters/{id}/voice/clone) and narration B-roll
suggest (POST /projects/{id}/broll-suggest). The dialogue POST accepts an
optional `Idempotency-Key` header like the generation POSTs (design §12.2).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import audio as s
from app.schemas import audio_p1 as p1
from app.services import audio as audio_service
from app.services import broll as broll_service
from app.services import dialogue as dialogue_service
from app.services import idempotency

router = APIRouter(tags=["audio"])


@router.get("/projects/{project_id}/audio", response_model=list[s.AudioOut])
def list_audio(project_id: uuid.UUID, kind: str | None = None,
               db: Session = Depends(get_db)):
    return [
        s.AudioOut(id=a.id, project_id=a.project_id, kind=a.kind,
                   output_path=a.output_path, duration_sec=a.duration_sec,
                   status=a.status)
        for a in audio_service.list_audio(db, project_id, kind=kind)
    ]


@router.post("/projects/{project_id}/audio", response_model=s.AudioOut, status_code=201)
def register_audio(project_id: uuid.UUID, body: s.AudioRegisterIn,
                   db: Session = Depends(get_db)):
    a = audio_service.register_from_asset(
        db, body.asset_id, kind=body.kind, project_id=project_id)
    return s.AudioOut(id=a.id, project_id=a.project_id, kind=a.kind,
                      output_path=a.output_path, duration_sec=a.duration_sec,
                      status=a.status)


@router.get("/timelines/{timeline_id}/audio-clips", response_model=list[s.AudioClipOut])
def list_clips(timeline_id: uuid.UUID, db: Session = Depends(get_db)):
    return [
        s.AudioClipOut(id=c.id, timeline_id=c.timeline_id, audio_id=c.audio_id,
                       kind=c.kind, start_sec=c.start_sec,
                       duration_sec=c.duration_sec, gain_db=c.gain_db)
        for c in audio_service.list_clips(db, timeline_id)
    ]


@router.post("/timelines/{timeline_id}/audio-clips", response_model=s.AudioClipOut,
             status_code=201)
def place_clip(timeline_id: uuid.UUID, body: s.AudioClipCreate,
               db: Session = Depends(get_db)):
    c = audio_service.place_clip(
        db, timeline_id, audio_id=body.audio_id, start_sec=body.start_sec,
        duration_sec=body.duration_sec, gain_db=body.gain_db,
    )
    return s.AudioClipOut(id=c.id, timeline_id=c.timeline_id, audio_id=c.audio_id,
                          kind=c.kind, start_sec=c.start_sec,
                          duration_sec=c.duration_sec, gain_db=c.gain_db)


@router.delete("/timelines/{timeline_id}/audio-clips/{clip_id}", status_code=204)
def delete_clip(timeline_id: uuid.UUID, clip_id: uuid.UUID,
                db: Session = Depends(get_db)):
    audio_service.delete_clip(db, clip_id)
    return None


# --- Phase-5 audio (P1-5) ----------------------------------------------------


@router.post("/shots/{shot_id}/dialogue", response_model=p1.DialogueAccepted,
             status_code=202)
def create_dialogue(
    shot_id: uuid.UUID,
    body: p1.DialogueIn,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    """Dialogue TTS (design §8.1): 202 {job_id}. Emotion weights are stored
    on the job and passed to the provider; the stub ignores them."""
    return idempotency.run(
        db, request, idempotency_key, body=body, response_model=p1.DialogueAccepted,
        create=lambda: dialogue_service.enqueue_dialogue(
            db,
            shot_id,
            character_id=body.character_id,
            text=body.text,
            emotion_weights=body.emotion_weights,
            model_id=body.model_id,
            target_duration_sec=body.target_duration_sec,
        ),
    )


@router.get("/audio/{audio_id}", response_model=p1.AudioJobRead)
def read_audio_job(audio_id: uuid.UUID, db: Session = Depends(get_db)):
    """Read one audio (TTS) job: status, output path, cost, lipsync info."""
    row = dialogue_service.get_audio_job(db, audio_id)
    return p1.AudioJobRead(
        id=row.id,
        project_id=row.project_id,
        shot_id=row.shot_id,
        character_id=row.character_id,
        kind=row.kind,
        text=row.text,
        voice_id=row.voice_id,
        emotion_weights=row.emotion_weights,
        model_id=row.model_id,
        output_path=row.output_path,
        duration_sec=row.duration_sec,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        estimated_cost_usd=str(row.estimated_cost_usd)
        if row.estimated_cost_usd is not None else None,
        actual_cost_usd=str(row.actual_cost_usd)
        if row.actual_cost_usd is not None else None,
    )


@router.post("/characters/{character_id}/voice/clone",
             response_model=p1.VoiceCloneOut)
def clone_voice(
    character_id: uuid.UUID,
    body: p1.VoiceCloneIn,
    db: Session = Depends(get_db),
) -> p1.VoiceCloneOut:
    """Voice clone from an uploaded audio sample (design §8.1, P1-5)."""
    voice_id = dialogue_service.clone_voice(
        db, character_id, body.sample_asset_id)
    return p1.VoiceCloneOut(voice_id=voice_id)


@router.post("/projects/{project_id}/broll-suggest",
             response_model=p1.BrollSuggestOut)
def broll_suggest(
    project_id: uuid.UUID,
    body: p1.BrollSuggestIn,
    db: Session = Depends(get_db),
) -> p1.BrollSuggestOut:
    """Narration-based B-roll auto-suggest (P1-5): keyword match of the
    timeline's subtitle/narration text against background/location assets."""
    res = broll_service.suggest_broll(
        db, project_id, body.timeline_id, limit=body.limit)
    return p1.BrollSuggestOut(
        suggestions=[p1.BrollSuggestion(**s) for s in res["suggestions"]],
        keywords=res["keywords"],
        narration_chars=res["narration_chars"],
    )
