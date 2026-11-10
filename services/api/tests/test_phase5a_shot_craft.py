"""Phase-5A completion proof: shot craft (P1-2, P1-3, P1-8, stub adapters).

Proves:
  1. prompt snippet CRUD + scope/category/tag filters,
  2. built-in camera presets are seeded (6),
  3. shot.seed pinning: explicit seed fixes it; frame/retake without a seed
     reuse the pinned seed; without any seed a fresh random seed is drawn,
  4. frame variations: seed axis (one batch, consecutive seeds) and prompt
     axis (N batches, marked prompts), count 2..4 validation, idempotency,
  5. 180-degree check: left<->right flips between consecutive shots are
     flagged; center/unknown sides are neutral,
  6. shot camera fields: camera_side validation, camera_preset_id 404.

Celery runs eager (task_always_eager), so 202 responses complete inline.
"""
from __future__ import annotations

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
from app.services.camera import ensure_builtin_camera_presets
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
from app.tasks.celery_app import celery_app

import app.main  # noqa: E402
from app.routers import camera_presets as camera_presets_router
from app.routers import snippets as snippets_router
from app.routers import variations as variations_router


def _ensure_phase5a_routers() -> None:
    """Register the Phase-5A routers for tests.

    main.py registration is done separately at merge time; this guard keeps
    the tests green both before and after that lands (no duplicates).
    """
    paths = {r.path for r in app.main.app.routes if hasattr(r, "path")}
    if "/api/v1/prompt-snippets" not in paths:
        app.main.app.include_router(snippets_router.router, prefix="/api/v1")
    if "/api/v1/camera-presets" not in paths:
        app.main.app.include_router(camera_presets_router.router, prefix="/api/v1")
    if "/api/v1/shots/{shot_id}/frame/variations" not in paths:
        app.main.app.include_router(variations_router.router, prefix="/api/v1")


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
        assert ensure_builtin_camera_presets(db) == 6

    celery_app.conf.task_always_eager = True
    _ensure_phase5a_routers()
    client = TestClient(app.main.app)
    yield client, tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _project_scene_shot(client: TestClient):
    pid = client.post("/api/v1/projects", json={"title": "5A"}).json()["id"]
    sid = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "S1"}).json()["id"]
    shot = client.post(f"/api/v1/scenes/{sid}/shots", json={"action": "hero walks"}).json()
    return pid, sid, shot["id"]


def _child_seeds(parent_id: str) -> list[int]:
    with session_scope() as db:
        rows = (
            db.query(Generation)
            .filter(
                Generation.parent_generation_id == uuid.UUID(parent_id),
                Generation.is_batch.is_(False),
            )
            .order_by(Generation.batch_index)
            .all()
        )
        return [r.seed for r in rows]


# --- P1-8 prompt snippets ----------------------------------------------------


def test_snippet_crud_and_filters(api_env):
    client, _ = api_env
    pid, _, _ = _project_scene_shot(client)

    g = client.post(
        "/api/v1/prompt-snippets",
        json={"title": "네온 조명", "body": "neon rim lighting", "category": "조명",
              "tags": ["night", "city"]},
    )
    assert g.status_code == 201, g.text
    gid = g.json()["id"]
    assert g.json()["project_id"] is None

    p = client.post(
        "/api/v1/prompt-snippets",
        json={"title": "프로젝트 전용", "body": "project look", "project_id": pid,
              "tags": ["city"]},
    )
    assert p.status_code == 201, p.text
    pid_snippet = p.json()["id"]
    assert p.json()["project_id"] == pid

    assert len(client.get("/api/v1/prompt-snippets").json()) == 2
    assert [s["id"] for s in client.get("/api/v1/prompt-snippets?scope=global").json()] == [gid]
    scoped = client.get(f"/api/v1/prompt-snippets?scope=project&project_id={pid}").json()
    assert [s["id"] for s in scoped] == [pid_snippet]
    r = client.get("/api/v1/prompt-snippets?scope=project")
    assert r.status_code == 400, r.text  # project_id required

    assert [s["id"] for s in client.get("/api/v1/prompt-snippets?category=조명").json()] == [gid]
    tagged = client.get("/api/v1/prompt-snippets?tag=city").json()
    assert {s["id"] for s in tagged} == {gid, pid_snippet}

    r = client.get(f"/api/v1/prompt-snippets/{gid}")
    assert r.json()["title"] == "네온 조명"
    r = client.patch(f"/api/v1/prompt-snippets/{gid}", json={"title": "네온", "tags": ["night"]})
    assert r.json()["title"] == "네온" and r.json()["tags"] == ["night"]

    r = client.delete(f"/api/v1/prompt-snippets/{gid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/prompt-snippets/{gid}").status_code == 404
    # unknown project -> 404
    r = client.post(
        "/api/v1/prompt-snippets",
        json={"title": "x", "body": "y", "project_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404, r.text


# --- P1-2 camera presets -------------------------------------------------------


def test_camera_presets_seeded(api_env):
    client, _ = api_env
    presets = client.get("/api/v1/camera-presets").json()
    assert len(presets) == 6
    names = {p["name"] for p in presets}
    assert "정적 아이레벨" in names and "더치 앵글" in names
    assert all(p["is_builtin"] for p in presets)


def test_shot_camera_fields_validation(api_env):
    client, _ = api_env
    _, sid, shot_id = _project_scene_shot(client)
    preset_id = client.get("/api/v1/camera-presets").json()[0]["id"]

    r = client.patch(f"/api/v1/shots/{shot_id}", json={"camera_side": "up"})
    assert r.status_code == 400, r.text
    r = client.patch(f"/api/v1/shots/{shot_id}", json={"camera_preset_id": str(uuid.uuid4())})
    assert r.status_code == 404, r.text

    r = client.post(
        f"/api/v1/scenes/{sid}/shots",
        json={"action": "close up", "camera_side": "left", "camera_preset_id": preset_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["camera_side"] == "left"
    assert body["camera_preset_id"] == preset_id
    assert body["seed"] is None


def test_camera_check_180_degree(api_env):
    client, _ = api_env
    _, sid, _ = _project_scene_shot(client)
    ids = []
    for side in ["left", "left", "right", None]:
        r = client.post(
            f"/api/v1/scenes/{sid}/shots",
            json={"action": "beat", "camera_side": side} if side else {"action": "beat"},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    # ids[0] is the fixture shot (side None); shots 1..3 set left/left/right.
    r = client.patch(f"/api/v1/shots/{ids[0]}", json={"camera_side": "left"})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/v1/scenes/{sid}/camera-check")
    assert r.status_code == 200, r.text
    violations = r.json()["violations"]
    assert len(violations) == 1, violations
    assert violations[0]["shot_id"] == ids[2]
    assert "180도" in violations[0]["message"]

    # center is neutral: left -> center -> right is not a violation.
    for i, side in enumerate(["center", "right"]):
        client.patch(f"/api/v1/shots/{ids[i + 1]}", json={"camera_side": side})
    r = client.get(f"/api/v1/scenes/{sid}/camera-check")
    assert r.json()["violations"] == [], r.text

    r = client.get(f"/api/v1/scenes/{uuid.uuid4()}/camera-check")
    assert r.status_code == 404, r.text


# --- P1-3 seed pinning ---------------------------------------------------------


def test_seed_pin_and_reuse(api_env):
    client, _ = api_env
    _, _, shot_id = _project_scene_shot(client)

    # Explicit seed pins shot.seed; children get seed+i.
    r = client.post(f"/api/v1/shots/{shot_id}/frame", json={"seed": 42, "batch_size": 2})
    assert r.status_code == 202, r.text
    assert _child_seeds(r.json()["job_id"]) == [42, 43]
    assert client.get(f"/api/v1/shots/{shot_id}").json()["seed"] == 42

    # Retake without a seed reuses the pinned seed.
    r = client.post(f"/api/v1/shots/{shot_id}/retake", json={})
    assert r.status_code == 202, r.text
    assert _child_seeds(r.json()["job_id"]) == [42]

    # Explicit seed on retake re-pins.
    r = client.post(f"/api/v1/shots/{shot_id}/retake", json={"seed": 100})
    assert r.status_code == 202, r.text
    assert _child_seeds(r.json()["job_id"]) == [100]
    assert client.get(f"/api/v1/shots/{shot_id}").json()["seed"] == 100


def test_seed_via_patch_and_random_fallback(api_env):
    client, _ = api_env
    _, _, shot_id = _project_scene_shot(client)

    r = client.patch(f"/api/v1/shots/{shot_id}", json={"seed": 7})
    assert r.json()["seed"] == 7
    r = client.post(f"/api/v1/shots/{shot_id}/frame", json={})
    assert _child_seeds(r.json()["job_id"]) == [7]

    # No seed anywhere: fresh random seeds (non-null ints).
    _, _, shot2 = _project_scene_shot(client)
    r = client.post(f"/api/v1/shots/{shot2}/frame", json={"batch_size": 2})
    seeds = _child_seeds(r.json()["job_id"])
    assert len(seeds) == 2 and all(isinstance(s, int) for s in seeds)
    assert client.get(f"/api/v1/shots/{shot2}").json()["seed"] is None


# --- P1-3 variations -----------------------------------------------------------


def test_variations_seed_axis(api_env):
    client, _ = api_env
    _, _, shot_id = _project_scene_shot(client)

    r = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations",
        json={"count": 3, "variation_axis": "seed", "seed": 500},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["count"] == 3 and body["variation_axis"] == "seed"
    assert body["estimated_cost_usd"] is not None
    assert len(body["variation_job_ids"]) == 1  # one parent batch
    assert [v["seed"] for v in body["variations"]] == [500, 501, 502]
    assert all(v["job_id"] == body["job_id"] for v in body["variations"])
    assert _child_seeds(body["job_id"]) == [500, 501, 502]
    # Explicit seed pinned the shot.
    assert client.get(f"/api/v1/shots/{shot_id}").json()["seed"] == 500


def test_variations_prompt_axis(api_env):
    client, _ = api_env
    _, _, shot_id = _project_scene_shot(client)

    r = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations",
        json={"count": 2, "variation_axis": "prompt"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["variation_job_ids"]) == 2  # N single-image batches
    prompts = [v["prompt_final"] for v in body["variations"]]
    assert "[variation 1 of 2" in prompts[0] and "[variation 2 of 2" in prompts[1]
    assert prompts[0] != prompts[1]
    seeds = [v["seed"] for v in body["variations"]]
    # F15 / ADR-0014: one variable at a time - prompt varies, seed is fixed.
    assert len(set(seeds)) == 1


def test_variations_validation_and_idempotency(api_env):
    client, _ = api_env
    _, _, shot_id = _project_scene_shot(client)

    r = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations", json={"count": 5}
    )
    assert r.status_code == 400, r.text  # app maps request validation to 400
    r = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations",
        json={"count": 2, "variation_axis": "bogus"},
    )
    assert r.status_code == 400, r.text

    headers = {"Idempotency-Key": "var-key-1"}
    r1 = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations",
        json={"count": 2, "variation_axis": "seed"},
        headers=headers,
    )
    r2 = client.post(
        f"/api/v1/shots/{shot_id}/frame/variations",
        json={"count": 2, "variation_axis": "seed"},
        headers=headers,
    )
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]
    with session_scope() as db:
        n = (
            db.query(Generation)
            .filter(Generation.shot_id == uuid.UUID(shot_id), Generation.is_batch.is_(True))
            .count()
        )
    assert n == 1  # the replay created no new jobs
