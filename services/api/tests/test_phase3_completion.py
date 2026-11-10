"""Phase-3 completion proof (stub adapters, no API keys).

Proves the Phase-3 acceptance criteria end to end: 5 approved stills ->
  5 I2V clips -> 5-clip timeline -> one rendered MP4.
  1. unapproved shot -> I2V raises 409 APPROVAL_REQUIRED,
  2. motion_prompt is required (422) and duration clamps to the model's
     max_duration_sec (60s -> 10s for the stub),
  3. approved I2V -> stub mp4 saved at
     projects/{pid}/scenes/{sid}/shots/{shot}/videos/{video}.mp4,
     shot status video_done, clip history lists the video,
  4. timeline build-from-shots auto-arranges the 5 shots' latest done clips
     in shot order (timeline_clips rows, positions 0-4),
  5. PATCH reorder/trim/delete validation (empty range -> 400, unknown
     video -> 400),
  6. rendering locks the timeline: PATCH and re-render -> 409 RENDER_LOCKED,
  7. export presets: 4 read-only built-ins; custom CRUD; built-in edit -> 400,
  8. render -> output_<attempt>.mp4, ffprobe-verified: H.264, 24fps, 1920x1080
     (YouTube 16:9 preset), AAC 48kHz, ~9s from 5 stub clips,
  9. T2V needs no frame approval (unapproved shot -> clip generated).

Celery runs eager (task_always_eager), so 202 responses complete inline.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.generation import Generation
from app.models.production import Shot
from app.models.timeline import Timeline, TimelineClip
from app.services.exports import ensure_builtin_presets
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
from app.tasks import tasks_post  # noqa: F401 (registers the q.post dispatcher)
from app.tasks import tasks_video  # noqa: F401 (registers the q.video dispatcher)
from app.tasks.celery_app import celery_app

import app.main  # noqa: E402


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
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

    seed_dir = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "category_packs"
    with session_scope() as db:
        assert seed_category_packs(db, seed_dir) == 3
        assert ensure_builtin_presets(db) == 4

    celery_app.conf.task_always_eager = True
    client = TestClient(app.main.app)
    yield client, tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    import json as _json

    return _json.loads(out.stdout)


@pytest.mark.needs_ffmpeg
def test_phase3_completion(api_env):
    client, tmp_path = api_env

    # --- project / scene / 6 shots --------------------------------------
    r = client.post("/api/v1/projects", json={"title": "P3 proof"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "Opening"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    shot_ids: list[str] = []
    for i in range(6):
        r = client.post(
            f"/api/v1/scenes/{sid}/shots",
            json={"action": f"Hero walks forward, beat {i}", "duration_sec": 5},
        )
        assert r.status_code == 201, r.text
        shot_ids.append(r.json()["id"])

    # --- frames + approval for the first 5 shots -------------------------
    for s in shot_ids[:5]:
        r = client.post(f"/api/v1/shots/{s}/frame", json={"batch_size": 1})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        r = client.get(f"/api/v1/jobs/{job_id}")
        take = r.json()["children"][0]["id"]
        r = client.post(f"/api/v1/shots/{s}/frame/approve", json={"generation_id": take})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "frame_approved"

    # shot 6 gets frames but NO approval.
    r = client.post(f"/api/v1/shots/{shot_ids[5]}/frame", json={"batch_size": 1})
    assert r.status_code == 202, r.text

    # --- 1. approval gate: I2V 409 on unapproved --------------------------
    # Shot 6 has frames but no approval: I2V is blocked here; T2V needs no
    # approval and is proven separately in section 9 so the timeline below
    # assembles exactly the 5 approved I2V clips.
    r = client.post(
        f"/api/v1/shots/{shot_ids[5]}/video",
        json={"mode": "i2v", "motion_prompt": "camera pushes in"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "APPROVAL_REQUIRED"

    # --- 2. motion prompt required; duration clamped to model max --------
    r = client.post(f"/api/v1/shots/{shot_ids[0]}/video", json={"mode": "i2v"})
    assert r.status_code == 400, r.text  # motion_prompt is required

    r = client.post(
        f"/api/v1/shots/{shot_ids[0]}/video",
        json={"mode": "i2v", "motion_prompt": "hero blinks", "duration_sec": 60},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    video_id = r.json()["video_id"]
    assert "estimated_cost_usd" in r.json()
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(job_id))
        # Stub max_duration_sec is 10: the request is clamped at enqueue time.
        assert gen.params["duration_sec"] == 10, gen.params

    # --- 3. remaining 4 clips --------------------------------------------
    for s in shot_ids[1:5]:
        r = client.post(
            f"/api/v1/shots/{s}/video",
            json={"mode": "i2v", "motion_prompt": f"wind moves, beat {s[:4]}"},
        )
        assert r.status_code == 202, r.text

    for s in shot_ids[:5]:
        r = client.get(f"/api/v1/shots/{s}")
        assert r.json()["status"] == "video_done", r.json()
        r = client.get(f"/api/v1/shots/{s}/videos")
        assert r.status_code == 200, r.text
        videos = r.json()
        assert len(videos) == 1 and videos[0]["status"] == "done"
        rel = videos[0]["output_path"]
        assert rel.endswith(".mp4") and "/videos/" in rel
        assert (tmp_path / rel).is_file(), rel

    clip_path = tmp_path / client.get(f"/api/v1/shots/{shot_ids[0]}/videos").json()[0]["output_path"]
    facts = _ffprobe(clip_path)
    vstream = next(s for s in facts["streams"] if s["codec_type"] == "video")
    assert vstream["codec_name"] == "h264"

    # --- 7. export presets ------------------------------------------------
    r = client.get("/api/v1/export-presets")
    assert r.status_code == 200, r.text
    builtins = r.json()
    assert len(builtins) == 4 and all(p["builtin"] for p in builtins)
    youtube = next(p for p in builtins if p["platform"] == "youtube")
    assert youtube["resolution"] == "1920x1080" and youtube["fps"] == 24

    r = client.get("/api/v1/export-presets", params={"project_id": pid})
    assert len(r.json()) == 4  # built-ins only so far

    r = client.post(
        f"/api/v1/projects/{pid}/export-presets",
        json={"name": "Square 1:1", "aspect_ratio": "1:1", "resolution": "1080x1080"},
    )
    assert r.status_code == 200, r.text
    custom_id = r.json()["id"]
    assert r.json()["builtin"] is False

    r = client.patch(f"/api/v1/export-presets/{youtube['id']}", json={"name": "X"})
    assert r.status_code == 400, r.text  # built-ins are immutable

    r = client.patch(f"/api/v1/export-presets/{custom_id}", json={"fps": 30})
    assert r.status_code == 200 and r.json()["fps"] == 30, r.text

    r = client.get("/api/v1/export-presets", params={"project_id": pid})
    assert len(r.json()) == 5

    r = client.delete(f"/api/v1/export-presets/{custom_id}")
    assert r.status_code == 204, r.text

    # --- 4. timeline: build-from-shots ------------------------------------
    r = client.post(f"/api/v1/projects/{pid}/timelines", json={"name": "Cut 1"})
    assert r.status_code == 201, r.text  # creation -> 201 (Phase 4 contract fix)
    tid = r.json()["id"]

    r = client.post(f"/api/v1/timelines/{tid}/build-from-shots")
    assert r.status_code == 200, r.text
    clips = r.json()["clip_order"]
    # F-12: each of the 5 approved shots contributes its latest done I2V
    # clip, in shot order (shot 6's T2V comes later in section 9).
    assert len(clips) == 5, clips
    with session_scope() as db:
        rows = (
            db.query(TimelineClip)
            .filter(TimelineClip.timeline_id == uuid.UUID(tid))
            .order_by(TimelineClip.position)
            .all()
        )
        assert [r.position for r in rows] == [0, 1, 2, 3, 4]
        assert [str(r.shot_id) for r in rows] == shot_ids[:5]

    # --- 5. PATCH: reorder + trim ------------------------------------------
    reversed_clips = list(reversed(clips))
    reversed_clips[0]["in_sec"] = 0.5
    reversed_clips[0]["out_sec"] = 1.5
    r = client.patch(f"/api/v1/timelines/{tid}", json={"clip_order": reversed_clips})
    assert r.status_code == 200, r.text
    got = r.json()["clip_order"]
    assert got[0]["video_id"] == reversed_clips[0]["video_id"]
    assert got[0]["in_sec"] == 0.5 and got[0]["out_sec"] == 1.5

    # Empty range -> 400.
    bad = [dict(c) for c in got]
    bad[1]["out_sec"] = bad[1]["in_sec"]
    r = client.patch(f"/api/v1/timelines/{tid}", json={"clip_order": bad})
    assert r.status_code == 400, r.text

    # Unknown video -> 400.
    bad = [dict(c) for c in got]
    bad[1]["video_id"] = str(uuid.uuid4())
    r = client.patch(f"/api/v1/timelines/{tid}", json={"clip_order": bad})
    assert r.status_code == 400, r.text

    # --- 6. render lock ------------------------------------------------------
    with session_scope() as db:
        tl = db.get(Timeline, uuid.UUID(tid))
        tl.status = "rendering"
        db.commit()
    r = client.patch(f"/api/v1/timelines/{tid}", json={"name": "locked?"})
    assert r.status_code == 409 and r.json()["code"] == "RENDER_LOCKED", r.text
    r = client.post(
        f"/api/v1/timelines/{tid}/render",
        json={"export_preset_id": youtube["id"]},
    )
    assert r.status_code == 409 and r.json()["code"] == "RENDER_LOCKED", r.text
    with session_scope() as db:
        tl = db.get(Timeline, uuid.UUID(tid))
        tl.status = "draft"
        db.commit()

    # --- 7b. QC gate (Phase 4, ADR-0006): render requires a fresh QC report --
    r = client.post(f"/api/v1/timelines/{tid}/qc")
    assert r.status_code == 202, r.text
    qid = r.json()["qc_report_id"]
    r = client.get(f"/api/v1/qc-reports/{qid}")
    assert r.status_code == 200, r.text
    assert r.json()["overall"] in ("pass", "warn"), r.text
    assert len(r.json()["checks"]) == 15  # 13 format + 2 advisory content (P2)

    # --- 8. render -----------------------------------------------------------
    r = client.post(
        f"/api/v1/timelines/{tid}/render",
        json={"export_preset_id": youtube["id"], "ignore_warnings": True},
    )
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] == tid  # render jobs are tracked as timeline state

    r = client.get(f"/api/v1/timelines/{tid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done", body
    # Plan D8: each render publishes an immutable per-attempt file.
    import re

    assert re.search(r"/timelines/[0-9a-f-]+/output_[0-9a-f]{8}\.mp4$", body["output_path"])
    out_path = tmp_path / body["output_path"]
    assert out_path.is_file()

    facts = _ffprobe(out_path)
    vstream = next(s for s in facts["streams"] if s["codec_type"] == "video")
    astream = next(s for s in facts["streams"] if s["codec_type"] == "audio")
    assert vstream["codec_name"] == "h264"
    assert vstream["width"] == 1920 and vstream["height"] == 1080
    num, _, den = vstream["avg_frame_rate"].partition("/")
    assert abs(float(num) / float(den) - 24.0) < 0.01
    assert astream["codec_name"] == "aac"
    assert astream["sample_rate"] == "48000"
    # 5 clips x 2s stub = 10s (one clip trimmed to 1s -> 9s).
    duration = float(facts["format"]["duration"])
    assert 8.5 < duration < 10.5, duration

    # Output download serves the mp4.
    r = client.get(f"/api/v1/timelines/{tid}/output")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4", r.text[:200]

    # --- 9. T2V needs no frame approval ------------------------------------
    r = client.post(
        f"/api/v1/shots/{shot_ids[5]}/video",
        json={"mode": "t2v", "motion_prompt": "city lights flicker"},
    )
    assert r.status_code == 202, r.text  # T2V allowed without approval
    r = client.get(f"/api/v1/shots/{shot_ids[5]}")
    assert r.json()["status"] == "video_done", r.json()
    r = client.get(f"/api/v1/shots/{shot_ids[5]}/videos")
    videos = r.json()
    assert len(videos) == 1 and videos[0]["status"] == "done"
    assert videos[0]["mode"] == "t2v"


def test_render_unexpected_failure_unlocks_timeline(api_env, monkeypatch):
    """A non-StudioError during render must not leave the timeline stuck in
    "rendering": the worker safety net marks it failed (PROVIDER_ERROR)."""
    import app.services.render as render_service
    from app.tasks import tasks_post

    client, tmp_path = api_env

    r = client.post("/api/v1/projects", json={"title": "P3 crash proof"})
    pid = r.json()["id"]
    r = client.post(f"/api/v1/projects/{pid}/timelines", json={"name": "Cut 1"})
    tid = r.json()["id"]
    with session_scope() as db:
        db.get(Timeline, uuid.UUID(tid)).status = "rendering"
        db.commit()

    monkeypatch.setattr(
        render_service, "render_timeline",
        lambda _tid: (_ for _ in ()).throw(RuntimeError("disk exploded")),
    )
    with __import__("pytest").raises(RuntimeError):
        tasks_post.render_timeline.run(tid)

    with session_scope() as db:
        tl = db.get(Timeline, uuid.UUID(tid))
        assert tl.status == "failed"
        assert tl.error_code == "PROVIDER_ERROR"
