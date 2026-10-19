"""Phase-6C completion proof: 3D camera previz (P2, ADR-0010).

Proves:
  1. camera keyframe CRUD (create/list/update/delete),
  2. duplicate (shot_id, t_sec) -> 422,
  3. negative t_sec -> 422,
  4. unknown shot / keyframe -> 404,
  5. camera-path linear interpolation accuracy:
     - midpoint of two keyframes is the exact average,
     - samples span [first.t_sec, last.t_sec] evenly,
     - empty shot -> zero samples, single keyframe -> one sample.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine

import app.main  # noqa: E402
from app.routers import previz as previz_router


def _ensure_previz_router() -> None:
    """Register the previz router for tests.

    main.py registration is done separately at merge time; this guard keeps
    the tests green both before and after that lands (no duplicates).
    """
    paths = {r.path for r in app.main.app.routes if hasattr(r, "path")}
    if "/api/v1/shots/{shot_id}/camera-keyframes" not in paths:
        app.main.app.include_router(previz_router.router, prefix="/api/v1")


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

    _ensure_previz_router()
    client = TestClient(app.main.app)
    yield client, tmp_path


def _mk_shot(client: TestClient):
    pid = client.post("/api/v1/projects", json={"title": "6C"}).json()["id"]
    sid = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "S1"}).json()["id"]
    shot = client.post(f"/api/v1/scenes/{sid}/shots", json={"action": "pan"}).json()
    return shot["id"]


def _mk_kf(client: TestClient, shot_id: str, t_sec: float,
           px: float = 0.0, pz: float = 5.0, fov: float = 40.0):
    return client.post(
        f"/api/v1/shots/{shot_id}/camera-keyframes",
        json={
            "t_sec": t_sec,
            "position": {"x": px, "y": 1.6, "z": pz},
            "target": {"x": 0.0, "y": 1.2, "z": 0.0},
            "fov_deg": fov,
        },
    )


def test_keyframe_crud(api_env):
    client, _ = api_env
    shot_id = _mk_shot(client)

    r = _mk_kf(client, shot_id, 0.0)
    assert r.status_code == 201, r.text
    kf_id = r.json()["id"]
    assert r.json()["t_sec"] == 0.0

    r = _mk_kf(client, shot_id, 2.5, px=3.0)
    assert r.status_code == 201, r.text

    # List is ordered by t_sec.
    items = client.get(f"/api/v1/shots/{shot_id}/camera-keyframes").json()
    assert [k["t_sec"] for k in items] == [0.0, 2.5]

    # Update.
    r = client.patch(f"/api/v1/camera-keyframes/{kf_id}",
                     json={"fov_deg": 50.0})
    assert r.status_code == 200, r.text
    assert r.json()["fov_deg"] == 50.0

    # Delete.
    r = client.delete(f"/api/v1/camera-keyframes/{kf_id}")
    assert r.status_code == 204
    items = client.get(f"/api/v1/shots/{shot_id}/camera-keyframes").json()
    assert len(items) == 1


def test_duplicate_t_sec_rejected(api_env):
    client, _ = api_env
    shot_id = _mk_shot(client)
    assert _mk_kf(client, shot_id, 1.0).status_code == 201
    r = _mk_kf(client, shot_id, 1.0)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "DUPLICATE_KEYFRAME"

    # PATCHing onto an occupied t_sec is also rejected.
    kf2 = _mk_kf(client, shot_id, 2.0).json()
    r = client.patch(f"/api/v1/camera-keyframes/{kf2['id']}", json={"t_sec": 1.0})
    assert r.status_code == 422


def test_negative_t_sec_rejected(api_env):
    client, _ = api_env
    shot_id = _mk_shot(client)
    # Schema-level ge=0 -> 400 VALIDATION_ERROR (request validation).
    r = _mk_kf(client, shot_id, -0.5)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_unknown_ids_404(api_env):
    client, _ = api_env
    import uuid

    shot_id = _mk_shot(client)
    r = client.get(f"/api/v1/shots/{uuid.uuid4()}/camera-keyframes")
    assert r.status_code == 404
    r = client.patch(f"/api/v1/camera-keyframes/{uuid.uuid4()}",
                     json={"fov_deg": 35.0})
    assert r.status_code == 404
    r = client.delete(f"/api/v1/camera-keyframes/{uuid.uuid4()}")
    assert r.status_code == 404
    r = client.get(f"/api/v1/shots/{uuid.uuid4()}/camera-path")
    assert r.status_code == 404
    # Path on a real shot with no keyframes is fine (empty samples).
    r = client.get(f"/api/v1/shots/{shot_id}/camera-path")
    assert r.status_code == 200
    assert r.json()["samples"] == []
    assert r.json()["keyframe_count"] == 0


def test_camera_path_interpolation(api_env):
    client, _ = api_env
    shot_id = _mk_shot(client)
    # Keyframe A: x=0, fov 40 at t=0. Keyframe B: x=10, fov 60 at t=4.
    assert _mk_kf(client, shot_id, 0.0, px=0.0, fov=40.0).status_code == 201
    assert _mk_kf(client, shot_id, 4.0, px=10.0, fov=60.0).status_code == 201

    path = client.get(
        f"/api/v1/shots/{shot_id}/camera-path", params={"samples": 5}
    ).json()
    assert path["keyframe_count"] == 2
    samples = path["samples"]
    assert len(samples) == 5
    # Evenly spaced across [0, 4].
    assert [s["t_sec"] for s in samples] == [0.0, 1.0, 2.0, 3.0, 4.0]
    # Midpoint is the exact average (linear interpolation).
    mid = samples[2]
    assert mid["position"]["x"] == pytest.approx(5.0)
    assert mid["position"]["y"] == pytest.approx(1.6)
    assert mid["target"]["z"] == pytest.approx(0.0)
    assert mid["fov_deg"] == pytest.approx(50.0)
    # Endpoints match the keyframes.
    assert samples[0]["position"]["x"] == pytest.approx(0.0)
    assert samples[-1]["position"]["x"] == pytest.approx(10.0)
    assert samples[-1]["fov_deg"] == pytest.approx(60.0)


def test_camera_path_single_keyframe(api_env):
    client, _ = api_env
    shot_id = _mk_shot(client)
    assert _mk_kf(client, shot_id, 1.5, px=2.0).status_code == 201
    path = client.get(f"/api/v1/shots/{shot_id}/camera-path").json()
    assert path["keyframe_count"] == 1
    assert len(path["samples"]) == 1
    assert path["samples"][0]["t_sec"] == pytest.approx(1.5)
    assert path["samples"][0]["position"]["x"] == pytest.approx(2.0)
