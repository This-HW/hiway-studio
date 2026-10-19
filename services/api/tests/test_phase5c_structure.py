"""Phase-5C completion proof (P1-10 episode templates, P1-6 subtitle editing).

  1. Episode CRUD: create -> list -> get -> patch -> delete -> 404.
  2. POST /episodes/{id}/clone deep copy (D-23): new ids everywhere, cloned
     shots are draft with no approved generation, generations/videos/audio
     rows are NOT copied (original rows unchanged).
  3. POST /subtitle-tracks/{id}/apply-to-timeline: cue text length / CPS ->
     per-clip duration proposals; cue text longer than its clip raises
     warnings[]; the timeline itself is NOT rewritten (analysis only).
  4. Edge cases: timeline with no clips, cue matching no clip,
     track/timeline from different projects -> 400.

The test mounts a fresh FastAPI app with only the new routers so it is
independent of main.py router registration (the parent registers them at
the end of Phase 5).
"""
from __future__ import annotations

import uuid

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
from app.models.generation import Generation, Video
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.story import Episode
from app.models.subtitle import SubtitleCue, SubtitleTrack
from app.models.timeline import Timeline, TimelineClip
from app.routers import episodes as episodes_router
from app.routers import subtitles as subtitles_router


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
    api.include_router(episodes_router.router, prefix="/api/v1")
    api.include_router(subtitles_router.router, prefix="/api/v1")
    c = TestClient(api)
    yield c
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _project() -> uuid.UUID:
    with session_scope() as db:
        p = Project(id=uuid.uuid4(), title="P5C", aspect_ratio="16:9")
        db.add(p)
        db.flush()
        return p.id


def _episode_with_content(project_id: uuid.UUID) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Episode with 1 scene, 2 shots, a generation + video on shot 1."""
    with session_scope() as db:
        ep = Episode(id=uuid.uuid4(), project_id=project_id, order_index=0,
                     title="Ep1", status="in_progress")
        db.add(ep)
        db.flush()
        scene = Scene(id=uuid.uuid4(), project_id=project_id,
                      episode_id=ep.id, order_index=0, title="S1")
        db.add(scene)
        db.flush()
        shot_ids = []
        for i in range(2):
            shot = Shot(id=uuid.uuid4(), scene_id=scene.id, order_index=i,
                        shot_type="wide", duration_sec=5.0, dialogue=f"대사 {i}",
                        status="frame_approved")
            db.add(shot)
            db.flush()
            shot_ids.append(shot.id)
        gen = Generation(id=uuid.uuid4(), project_id=project_id,
                         job_type="text_to_image", shot_id=shot_ids[0],
                         status="succeeded", model_id="stub-image",
                         output_path="projects/x/frames/f.png")
        db.add(gen)
        db.flush()
        shot = db.get(Shot, shot_ids[0])
        shot.approved_frame_generation_id = gen.id
        video = Video(id=uuid.uuid4(), shot_id=shot_ids[0], generation_id=gen.id,
                      mode="i2v", duration_sec=5.0, fps=24,
                      resolution="1920x1080", model_id="stub-video",
                      output_path="projects/x/videos/v.mp4", status="done")
        db.add(video)
        db.flush()
        return ep.id, shot_ids


# --- P1-10: episode CRUD ------------------------------------------------------


def test_episode_crud(client):
    project_id = _project()
    r = client.post(f"/api/v1/projects/{project_id}/episodes",
                    json={"title": "Ep A", "is_template": True})
    assert r.status_code == 201, r.text
    ep_id = r.json()["id"]
    assert r.json()["status"] == "draft"
    assert r.json()["is_template"] is True

    r = client.get(f"/api/v1/projects/{project_id}/episodes")
    assert r.status_code == 200 and len(r.json()) == 1

    r = client.get(f"/api/v1/episodes/{ep_id}")
    assert r.status_code == 200 and r.json()["title"] == "Ep A"

    r = client.patch(f"/api/v1/episodes/{ep_id}",
                     json={"title": "Ep A2", "status": "in_progress"})
    assert r.status_code == 200
    assert r.json()["title"] == "Ep A2"
    assert r.json()["status"] == "in_progress"

    r = client.patch(f"/api/v1/episodes/{ep_id}", json={"status": "bogus"})
    assert r.status_code == 400

    r = client.delete(f"/api/v1/episodes/{ep_id}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/episodes/{ep_id}").status_code == 404


def test_episode_create_unknown_project_404(client):
    r = client.post(f"/api/v1/projects/{uuid.uuid4()}/episodes",
                    json={"title": "x"})
    assert r.status_code == 404


# --- P1-10: clone deep copy ---------------------------------------------------


def test_episode_clone_deep_copy(client):
    project_id = _project()
    ep_id, shot_ids = _episode_with_content(project_id)

    with session_scope() as db:
        gen_count = db.query(Generation).count()
        video_count = db.query(Video).count()

    r = client.post(f"/api/v1/episodes/{ep_id}/clone")
    assert r.status_code == 201, r.text
    clone_id = uuid.UUID(r.json()["episode_id"])
    assert clone_id != ep_id

    with session_scope() as db:
        clone = db.get(Episode, clone_id)
        assert clone.status == "draft"
        assert clone.template_source_episode_id == ep_id
        assert clone.title == "Ep1 (copy)"

        scenes = db.query(Scene).filter(Scene.episode_id == clone_id).all()
        assert len(scenes) == 1
        clone_shots = (
            db.query(Shot).filter(Shot.scene_id == scenes[0].id)
            .order_by(Shot.order_index).all()
        )
        assert len(clone_shots) == 2
        clone_shot_ids = {s.id for s in clone_shots}
        # New ids everywhere.
        assert not (clone_shot_ids & set(shot_ids))
        # Cloned shots are draft, regenerating — no approved generation.
        for s in clone_shots:
            assert s.status == "draft"
            assert s.approved_frame_generation_id is None
            assert s.dialogue in {"대사 0", "대사 1"}
        # Generations/videos/audio were NOT copied.
        assert db.query(Generation).count() == gen_count
        assert db.query(Video).count() == video_count
        assert db.query(Generation).filter(
            Generation.shot_id.in_(clone_shot_ids)).count() == 0
        assert db.query(Video).filter(
            Video.shot_id.in_(clone_shot_ids)).count() == 0
        # Source rows untouched.
        for sid in shot_ids:
            assert db.get(Shot, sid).status == "frame_approved"


def test_episode_clone_unknown_404(client):
    r = client.post(f"/api/v1/episodes/{uuid.uuid4()}/clone")
    assert r.status_code == 404


# --- P1-6: subtitle-driven edit analysis --------------------------------------


def _timeline_with_clips(project_id: uuid.UUID, shot_ids: list[uuid.UUID],
                         durations: list[float]) -> uuid.UUID:
    with session_scope() as db:
        tl = Timeline(id=uuid.uuid4(), project_id=project_id, name="TL1")
        db.add(tl)
        db.flush()
        for i, (sid, dur) in enumerate(zip(shot_ids, durations)):
            video = Video(id=uuid.uuid4(), shot_id=sid, mode="i2v",
                          duration_sec=dur, fps=24, resolution="1920x1080",
                          model_id="stub-video",
                          output_path=f"projects/x/videos/{i}.mp4", status="done")
            db.add(video)
            db.flush()
            db.add(TimelineClip(id=uuid.uuid4(), timeline_id=tl.id,
                                position=i, shot_id=sid, video_id=video.id,
                                in_sec=0.0, out_sec=dur))
        db.flush()
        return tl.id


def _track_with_cues(project_id: uuid.UUID,
                     cues: list[tuple[float, float, str]]) -> uuid.UUID:
    with session_scope() as db:
        track = SubtitleTrack(id=uuid.uuid4(), project_id=project_id,
                              language_code="ko", label="기본")
        db.add(track)
        db.flush()
        for start, end, text in cues:
            db.add(SubtitleCue(id=uuid.uuid4(), track_id=track.id,
                               start_sec=start, end_sec=end, text=text))
        db.flush()
        return track.id


def _clip_windows(timeline_id: uuid.UUID) -> list[tuple[float, float]]:
    with session_scope() as db:
        rows = (db.query(TimelineClip)
                .filter(TimelineClip.timeline_id == timeline_id)
                .order_by(TimelineClip.position).all())
        return [(r.in_sec, r.out_sec) for r in rows]


def test_subtitle_apply_to_timeline(client):
    project_id = _project()
    _, shot_ids = _episode_with_content(project_id)
    # Clips: shot0 = 4s, shot1 = 3s.
    tl_id = _timeline_with_clips(project_id, shot_ids, [4.0, 3.0])
    short_text = "안녕"  # 2 chars -> 2/12 = 0.17s: fits
    long_text = "가" * 120  # 120 chars -> 10s at cps=12: exceeds 3s clip
    track_id = _track_with_cues(project_id,
                                [(0.5, 1.5, short_text), (4.5, 6.0, long_text)])

    r = client.post(f"/api/v1/subtitle-tracks/{track_id}/apply-to-timeline",
                    json={"timeline_id": str(tl_id), "cps": 12.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_id"] == str(track_id)
    assert body["timeline_id"] == str(tl_id)
    adj = body["adjustments"]
    assert len(adj) == 2
    assert adj[0]["shot_id"] == str(shot_ids[0])
    assert adj[0]["current_dur_sec"] == 4.0
    assert adj[0]["suggested_dur_sec"] == pytest.approx(2 / 12.0, abs=0.01)
    assert adj[0]["fits"] is True
    assert adj[1]["shot_id"] == str(shot_ids[1])
    assert adj[1]["current_dur_sec"] == 3.0
    assert adj[1]["suggested_dur_sec"] == pytest.approx(10.0, abs=0.01)
    assert adj[1]["fits"] is False
    # Mismatch surfaced as a warning (dialogue longer than the cut).
    assert any("exceeds clip" in w for w in body["warnings"])

    # Analysis only: the timeline was NOT rewritten.
    assert _clip_windows(tl_id) == [(0.0, 4.0), (0.0, 3.0)]


def test_subtitle_apply_empty_timeline(client):
    project_id = _project()
    with session_scope() as db:
        tl = Timeline(id=uuid.uuid4(), project_id=project_id, name="empty")
        db.add(tl)
        db.flush()
        tl_id = tl.id
    track_id = _track_with_cues(project_id, [(0.0, 1.0, "안녕")])
    r = client.post(f"/api/v1/subtitle-tracks/{track_id}/apply-to-timeline",
                    json={"timeline_id": str(tl_id)})
    assert r.status_code == 200
    assert r.json()["adjustments"] == []
    assert any("no clips" in w for w in r.json()["warnings"])


def test_subtitle_apply_cue_matches_no_clip(client):
    project_id = _project()
    _, shot_ids = _episode_with_content(project_id)
    tl_id = _timeline_with_clips(project_id, shot_ids[:1], [2.0])
    # Cue sits after the timeline's total duration (2s).
    track_id = _track_with_cues(project_id, [(10.0, 11.0, "안녕")])
    r = client.post(f"/api/v1/subtitle-tracks/{track_id}/apply-to-timeline",
                    json={"timeline_id": str(tl_id)})
    assert r.status_code == 200
    assert r.json()["adjustments"] == []
    assert any("matches no timeline clip" in w for w in r.json()["warnings"])


def test_subtitle_apply_cross_project_400(client):
    project_id = _project()
    _, shot_ids = _episode_with_content(project_id)
    tl_id = _timeline_with_clips(project_id, shot_ids[:1], [2.0])
    other = _project()
    track_id = _track_with_cues(other, [(0.0, 1.0, "안녕")])
    r = client.post(f"/api/v1/subtitle-tracks/{track_id}/apply-to-timeline",
                    json={"timeline_id": str(tl_id)})
    assert r.status_code == 400


def test_subtitle_apply_unknown_ids_404(client):
    r = client.post(f"/api/v1/subtitle-tracks/{uuid.uuid4()}/apply-to-timeline",
                    json={"timeline_id": str(uuid.uuid4())})
    assert r.status_code == 404
