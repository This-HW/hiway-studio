"""Phase-6A completion proof (P2: XML/EDL export, world bible; ADR-0010).

  1. GET /timelines/{id}/export?format=edl: CMX3600 parse check — event
     count, track codes (V/A), record timecodes at 24fps, FROM CLIP NAME.
  2. GET /timelines/{id}/export?format=xml: well-formed FCP7 XML — sequence
     name, video/audio clipitems with correct start/end frames, pathurl.
  3. Determinism: two identical requests return byte-identical bodies.
  4. Empty timeline (no exportable clips) -> 400 VALIDATION_ERROR.
  5. World bible CRUD: create -> list (+category/tag/q filters) -> get ->
     patch -> delete -> 404. Invalid category -> 400. Unknown project -> 404.

The test mounts a fresh FastAPI app with only the new routers so it is
independent of main.py router registration (the parent registers them at
the end of Phase 6).
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.middleware import register_middleware
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.audio import AudioGeneration, TimelineAudioClip
from app.models.generation import Generation, Video
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.timeline import Timeline, TimelineClip
from app.models.world_bible import WorldBibleEntry
from app.routers import bible as bible_router
from app.routers import exports as exports_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()

    test_engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(sess_mod, "get_engine", lambda: test_engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    Base.metadata.create_all(test_engine)

    api = FastAPI()
    register_middleware(api)
    api.include_router(exports_router.router, prefix="/api/v1")
    api.include_router(bible_router.router, prefix="/api/v1")
    c = TestClient(api)
    yield c
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _timeline_with_media() -> tuple[uuid.UUID, uuid.UUID]:
    """Project -> scene -> 2 shots -> 2 videos; timeline with 2 clips (0-5s each)
    and 1 audio clip starting at 2s with 4s duration."""
    with session_scope() as db:
        project_id = uuid.uuid4()
        db.add(Project(id=project_id, title="P6A", aspect_ratio="16:9"))
        db.flush()
        scene = Scene(id=uuid.uuid4(), project_id=project_id,
                      order_index=0, title="S1")
        db.add(scene)
        db.flush()
        tl = Timeline(id=uuid.uuid4(), project_id=project_id, name="TL1")
        db.add(tl)
        db.flush()
        for i in range(2):
            shot = Shot(id=uuid.uuid4(), scene_id=scene.id, order_index=i,
                        shot_type="wide", duration_sec=5.0, status="frame_approved")
            db.add(shot)
            db.flush()
            video = Video(id=uuid.uuid4(), shot_id=shot.id, mode="i2v",
                          duration_sec=5.0, fps=24, resolution="1920x1080",
                          model_id="stub-video",
                          output_path=f"projects/p/shots/s{i}/videos/v{i}.mp4",
                          status="done")
            db.add(video)
            db.flush()
            db.add(TimelineClip(id=uuid.uuid4(), timeline_id=tl.id, position=i,
                                shot_id=shot.id, video_id=video.id,
                                in_sec=0.0, out_sec=5.0))
        audio = AudioGeneration(id=uuid.uuid4(), project_id=project_id, kind="bgm",
                                output_path="projects/p/audio/bgm.wav",
                                duration_sec=10.0, status="done")
        db.add(audio)
        db.flush()
        db.add(TimelineAudioClip(id=uuid.uuid4(), timeline_id=tl.id,
                                audio_id=audio.id, kind="bgm",
                                start_sec=2.0, duration_sec=4.0))
        db.flush()
        return project_id, tl.id


def _empty_timeline() -> uuid.UUID:
    with session_scope() as db:
        project_id = uuid.uuid4()
        db.add(Project(id=project_id, title="P6A-empty", aspect_ratio="16:9"))
        db.flush()
        tl = Timeline(id=uuid.uuid4(), project_id=project_id, name="Empty")
        db.add(tl)
        db.flush()
        return tl.id


# --- EDL ---------------------------------------------------------------------


def test_export_edl_events_and_timecodes(client):
    _, tl_id = _timeline_with_media()
    r = client.get(f"/api/v1/timelines/{tl_id}/export", params={"format": "edl"})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "TITLE: TL1"
    assert lines[1] == "FCM: NON-DROP FRAME"

    events = [ln for ln in lines if ln[:3].strip().isdigit()]
    assert len(events) == 3  # 2 video + 1 audio
    # 001: V, rec 00:00:00:00 -> 00:00:05:00
    assert " V " in events[0]
    assert "00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00" in events[0]
    # 002: V, rec 00:00:05:00 -> 00:00:10:00 (second clip starts at 5s)
    assert " V " in events[1]
    assert "00:00:05:00 00:00:10:00" in events[1]
    # 003: A, audio starts at 2s, 4s long
    assert " A " in events[2]
    assert "00:00:02:00 00:00:06:00" in events[2]

    names = [ln for ln in lines if ln.startswith("* FROM CLIP NAME:")]
    assert len(names) == 3
    assert "v0" in names[0] and "v1" in names[1] and "bgm" in names[2]


def test_export_edl_deterministic(client):
    _, tl_id = _timeline_with_media()
    a = client.get(f"/api/v1/timelines/{tl_id}/export", params={"format": "edl"})
    b = client.get(f"/api/v1/timelines/{tl_id}/export", params={"format": "edl"})
    assert a.status_code == 200 and b.status_code == 200
    assert a.content == b.content


def test_export_edl_empty_timeline_400(client):
    tl_id = _empty_timeline()
    r = client.get(f"/api/v1/timelines/{tl_id}/export", params={"format": "edl"})
    assert r.status_code == 400, r.text


def test_export_edl_unknown_timeline_404(client):
    r = client.get(f"/api/v1/timelines/{uuid.uuid4()}/export", params={"format": "edl"})
    assert r.status_code == 404, r.text


# --- FCP7 XML ----------------------------------------------------------------


def test_export_xml_wellformed_and_frames(client):
    _, tl_id = _timeline_with_media()
    r = client.get(f"/api/v1/timelines/{tl_id}/export", params={"format": "xml"})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]

    root = ET.fromstring(r.content)  # raises if not well-formed
    assert root.tag == "xmeml"
    seq = root.find("sequence")
    assert seq is not None and seq.get("name") == "TL1"

    vitems = seq.findall("./media/video/track/clipitem")
    assert len(vitems) == 2
    first, second = vitems
    # 5s at 24fps = 120 frames
    assert first.findtext("start") == "0" and first.findtext("end") == "120"
    assert first.findtext("in") == "0" and first.findtext("out") == "120"
    assert second.findtext("start") == "120" and second.findtext("end") == "240"
    pathurl = first.find("./file/pathurl").text
    assert pathurl == "file://./projects/p/shots/s0/videos/v0.mp4"

    aitems = seq.findall("./media/audio/track/clipitem")
    assert len(aitems) == 1
    aitem = aitems[0]
    # audio starts at 2s (48f), 4s long (96f)
    assert aitem.findtext("start") == "48" and aitem.findtext("end") == "144"


def test_export_xml_default_format_is_xml(client):
    _, tl_id = _timeline_with_media()
    r = client.get(f"/api/v1/timelines/{tl_id}/export")
    assert r.status_code == 200
    assert r.content.lstrip().startswith(b"<?xml")


# --- World bible ---------------------------------------------------------------


def _project() -> uuid.UUID:
    with session_scope() as db:
        pid = uuid.uuid4()
        db.add(Project(id=pid, title="P6A-bible", aspect_ratio="16:9"))
        db.flush()
        return pid


def test_bible_crud(client):
    project_id = _project()
    r = client.post(f"/api/v1/projects/{project_id}/bible", json={
        "category": "character", "title": "주인공",
        "body": "설명", "tags": ["주연"],
    })
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    assert r.json()["category"] == "character"

    r = client.post(f"/api/v1/projects/{project_id}/bible", json={
        "category": "location", "title": "마을", "body": "", "tags": [],
    })
    assert r.status_code == 201

    r = client.get(f"/api/v1/projects/{project_id}/bible")
    assert r.status_code == 200 and len(r.json()) == 2

    r = client.get(f"/api/v1/projects/{project_id}/bible", params={"category": "location"})
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["title"] == "마을"

    r = client.get(f"/api/v1/projects/{project_id}/bible", params={"tag": "주연"})
    assert r.status_code == 200 and len(r.json()) == 1

    r = client.get(f"/api/v1/projects/{project_id}/bible", params={"q": "주인공"})
    assert r.status_code == 200 and len(r.json()) == 1

    r = client.get(f"/api/v1/bible/{entry_id}")
    assert r.status_code == 200 and r.json()["title"] == "주인공"

    r = client.patch(f"/api/v1/bible/{entry_id}", json={"title": "주인공2", "body": "수정"})
    assert r.status_code == 200 and r.json()["title"] == "주인공2"

    r = client.delete(f"/api/v1/bible/{entry_id}")
    assert r.status_code == 204

    r = client.get(f"/api/v1/bible/{entry_id}")
    assert r.status_code == 404


def test_bible_invalid_category_400(client):
    project_id = _project()
    r = client.post(f"/api/v1/projects/{project_id}/bible", json={
        "category": "spaceship", "title": "X",
    })
    assert r.status_code == 400, r.text


def test_bible_unknown_project_404(client):
    r = client.get(f"/api/v1/projects/{uuid.uuid4()}/bible")
    assert r.status_code == 404, r.text
    r = client.post(f"/api/v1/projects/{uuid.uuid4()}/bible", json={
        "category": "rule", "title": "R",
    })
    assert r.status_code == 404, r.text


def test_bible_patch_invalid_category_400(client):
    project_id = _project()
    r = client.post(f"/api/v1/projects/{project_id}/bible", json={
        "category": "rule", "title": "R",
    })
    entry_id = r.json()["id"]
    r = client.patch(f"/api/v1/bible/{entry_id}", json={"category": "bogus"})
    assert r.status_code == 400, r.text
