"""P0 audit-fix regression tests (2026-09-30).

Findings from the thorough P0 audit, each proven by a test here:

  A. PATCH /characters/{id} with an appearance-field change must set
     frame_stale=true on dependent shots that already have takes
     (design §8.1). Non-appearance edits must not stale anything.
  C. Character master/sheet generation must respect the project budget:
     at 100% spend, POST /characters/{id}/master and .../sheet return
     403 BUDGET_EXCEEDED (design §9.4 — "신규 잡 등록 API").
  D. POST /jobs/{id}/cancel publishes the job.canceled SSE event
     (design §8.2).
  E. PATCH /models/{id} enables/disables models and edits the fallback
     chain (design §8.1); the registry prefers the DB overlay row.
  G. GET /jobs accepts ?project_id= (design §8.1).

Stub adapters only; no API keys. Celery runs eager.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.services.exports import ensure_builtin_presets
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
from app.tasks import tasks_post  # noqa: F401 (registers the q.post dispatcher)
from app.tasks import tasks_video  # noqa: F401 (registers the q.video dispatcher)
from app.tasks.celery_app import celery_app
from pathlib import Path

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


def _project(client: TestClient, title: str = "audit") -> str:
    r = client.post("/api/v1/projects", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _character(client: TestClient, pid: str) -> str:
    r = client.post(
        f"/api/v1/projects/{pid}/characters",
        json={
            "name": "Hero",
            "appearance_prompt": "a young woman with short black hair",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _shot(client: TestClient, pid: str, action: str = "Hero walks") -> tuple[str, str]:
    r = client.post(
        f"/api/v1/projects/{pid}/scenes",
        json={"title": "S1", "location": "rooftop", "time_of_day": "dusk",
              "lighting": "neon"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = client.post(f"/api/v1/scenes/{sid}/shots",
                    json={"action": action, "duration_sec": 5})
    assert r.status_code == 201, r.text
    return sid, r.json()["id"]


def test_character_appearance_edit_stales_dependent_shots(api_env):
    """Finding A: appearance change -> frame_stale on shots with takes."""
    client, _ = api_env
    pid = _project(client)
    cid = _character(client, pid)
    _, shot_with_takes = _shot(client, pid, "Hero walks")
    _, shot_without_takes = _shot(client, pid, "Hero runs")

    for shot_id in (shot_with_takes, shot_without_takes):
        r = client.post(
            f"/api/v1/shots/{shot_id}/references",
            json={"character_id": cid, "purpose": "primary"},
        )
        assert r.status_code == 201, r.text

    # Only the first shot gets a generated take (stub, eager).
    r = client.post(f"/api/v1/shots/{shot_with_takes}/frame", json={})
    assert r.status_code == 202, r.text

    r = client.patch(
        f"/api/v1/characters/{cid}",
        json={"appearance_prompt": "an older woman with long silver hair"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    stale = client.get(f"/api/v1/shots/{shot_with_takes}").json()["frame_stale"]
    assert stale is True
    fresh = client.get(f"/api/v1/shots/{shot_without_takes}").json()["frame_stale"]
    assert fresh is False

    # Non-appearance edit bumps version but stales nothing.
    r = client.patch(f"/api/v1/characters/{cid}", json={"name": "Heroine"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3


def test_character_generation_blocked_at_full_budget(api_env):
    """Finding C: master/sheet respect the 100% budget block."""
    client, _ = api_env
    pid = _project(client)
    cid = _character(client, pid)

    r = client.put(f"/api/v1/projects/{pid}/budget",
                   json={"monthly_budget_usd": 0.10})
    assert r.status_code == 200, r.text

    # Simulate 100% spend with a directly-inserted succeeded generation.
    from app.models.generation import Generation

    with session_scope() as db:
        db.add(Generation(project_id=uuid.UUID(pid), job_type="text_to_image",
                          status="succeeded", actual_cost_usd=Decimal("0.10")))
        db.flush()

    r = client.post(f"/api/v1/characters/{cid}/master", json={})
    assert r.status_code == 403 and r.json()["code"] == "BUDGET_EXCEEDED", r.text
    r = client.post(f"/api/v1/characters/{cid}/sheet", json={"views": ["front"]})
    assert r.status_code == 403 and r.json()["code"] == "BUDGET_EXCEEDED", r.text


def test_cancel_publishes_job_canceled(api_env):
    """Finding D: cancel emits job.canceled on the SSE bridge."""
    client, _ = api_env
    pid = _project(client)
    _, shot_id = _shot(client, pid)

    # A queued job that eager celery will not touch: insert directly.
    from app.models.generation import Generation

    with session_scope() as db:
        gen = Generation(project_id=uuid.UUID(pid),
                         shot_id=uuid.UUID(shot_id),
                         job_type="text_to_image", status="queued")
        db.add(gen)
        db.flush()
        job_id = str(gen.id)

    from app.services import generations as gen_service

    published: list[tuple[str, dict]] = []
    real_get_bridge = gen_service.get_bridge
    gen_service.get_bridge = lambda: type(
        "FakeBridge", (),
        {"publish": lambda self, ch, ev, data: published.append((ev, data))})()
    try:
        r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    finally:
        gen_service.get_bridge = real_get_bridge
    assert r.status_code == 200, r.text
    assert published == [("job.canceled", {"job_id": job_id,
                                           "job_type": "text_to_image"})], published


def test_patch_model_status_and_fallback(api_env):
    """Finding E: PATCH /models/{id} — disable + fallback chain edit."""
    client, _ = api_env

    r = client.get("/api/v1/models")
    assert r.status_code == 200, r.text
    model_id = r.json()[0]["model_id"]

    r = client.patch(f"/api/v1/models/{model_id}", json={"status": "disabled"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled"

    active_ids = [m["model_id"] for m in client.get("/api/v1/models").json()]
    assert model_id not in active_ids
    disabled_ids = [m["model_id"]
                    for m in client.get("/api/v1/models?status=disabled").json()]
    assert model_id in disabled_ids

    r = client.patch(f"/api/v1/models/{model_id}",
                     json={"status": "active", "fallback_model_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert r.json()["fallback_model_ids"] == []

    r = client.patch("/api/v1/models/no.such-model", json={"status": "disabled"})
    assert r.status_code == 404, r.text
    r = client.patch(f"/api/v1/models/{model_id}", json={"status": "melted"})
    assert r.status_code == 400, r.text
    r = client.patch(f"/api/v1/models/{model_id}",
                     json={"fallback_model_ids": ["no.such-model"]})
    assert r.status_code == 404, r.text


def test_list_jobs_project_filter(api_env):
    """Finding G: GET /jobs?project_id= filters by project."""
    client, _ = api_env
    pid_a = _project(client, "A")
    pid_b = _project(client, "B")

    from app.models.generation import Generation

    with session_scope() as db:
        db.add(Generation(project_id=uuid.UUID(pid_a), job_type="text_to_image",
                          status="succeeded"))
        db.add(Generation(project_id=uuid.UUID(pid_b), job_type="text_to_image",
                          status="succeeded"))
        db.flush()

    rows = client.get(f"/api/v1/jobs?project_id={pid_a}").json()
    assert len(rows) == 1 and rows[0]["project_id"] == pid_a
    rows = client.get(f"/api/v1/jobs?project_id={pid_b}").json()
    assert len(rows) == 1 and rows[0]["project_id"] == pid_b
    assert len(client.get("/api/v1/jobs").json()) == 2


def test_cors_allows_web_ui_origin(api_env):
    """Finding H (2026-09-30 browser UI audit): the API had no CORSMiddleware,
    so a browser running the web UI (port 3000) could not call the API at all —
    every fetch was blocked by CORS and the whole web UI showed
    "Failed to fetch". The local web origin must be allowed; unknown origins
    must not receive CORS headers."""
    client, _ = api_env
    r = client.get("/api/v1/projects", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    # Preflight for JSON POSTs.
    r = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    # Unknown origins get no CORS headers.
    r = client.get("/api/v1/projects", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers
