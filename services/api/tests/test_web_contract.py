"""Web <-> API contract (batch B6, findings C11/C12, ADR-0012).

1. No two routes share (method, path) — a duplicate silently shadows the
   later router (the old DELETE /references/{id} in scenes.py was dead).
2. Shot references are deleted under their shot: 204 for its own shot, 404
   for another shot's reference or a character-reference id.
3. Character read exposes `master_thumbnail` (the field the web reads).
4. Project list/detail carry the project-card stats (P0_SCREENS §1).
5. Every SSE event name the backend publishes is declared in
   packages/contracts/sse_events.json, and cost.alert's contract fields equal
   the keys budget_guard publishes.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.generation import Video
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
from app.tasks.celery_app import celery_app

import app.main  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[1] / "app"

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)


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

    with session_scope() as db:
        seed_category_packs(db, REPO / "infra" / "seeds" / "category_packs")

    celery_app.conf.task_always_eager = True
    client = TestClient(app.main.app)
    yield client, tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _write_png(root: Path, rel: str) -> str:
    abs_path = root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(_TINY_PNG)
    return rel


# --- 1. route uniqueness ------------------------------------------------------


def _api_routes(routes=None, prefix: str = "") -> list[tuple[str, str, str]]:
    """(method, full path, endpoint) for every APIRoute, walking included
    routers (FastAPI >= 0.16x wraps them lazily as _IncludedRouter, so
    app.routes alone would hide every /api/v1 route)."""
    out: list[tuple[str, str, str]] = []
    # A fresh app: some test files include extra routers into the shared
    # app.main.app, which would show up here as duplicates.
    for route in app.main.create_app().routes if routes is None else routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx_prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            out.extend(_api_routes(inner.routes, prefix + ctx_prefix))
        elif isinstance(route, APIRoute):
            for method in route.methods:
                out.append((method, prefix + route.path, route.endpoint.__name__))
    return out


def test_no_duplicate_method_path_routes():
    routes = _api_routes()
    # Guard against a vacuous pass if route discovery breaks again.
    assert len(routes) > 100, len(routes)
    seen: dict[tuple[str, str], str] = {}
    dupes: list[str] = []
    for method, path, endpoint in routes:
        key = (method, path)
        if key in seen:
            dupes.append(f"{method} {path}: {seen[key]} vs {endpoint}")
        else:
            seen[key] = endpoint
    assert dupes == []


def test_shot_reference_delete_route_is_registered():
    paths = {(m, path) for m, path, _ in _api_routes()}
    assert ("DELETE", "/api/v1/shots/{shot_id}/references/{reference_id}") in paths
    # The bare path stays the character-reference route (ADR-0012).
    assert ("DELETE", "/api/v1/references/{reference_id}") in paths


# --- 2/3. shot-reference delete + master_thumbnail ------------------------------


def _project_with_two_shots(client) -> tuple[str, str, str, str]:
    pid = client.post("/api/v1/projects", json={"title": "contract"}).json()["id"]
    sid = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "S"}).json()[
        "id"
    ]
    shots = []
    for i in range(2):
        r = client.post(f"/api/v1/scenes/{sid}/shots", json={"action": f"beat {i}"})
        assert r.status_code == 201, r.text
        shots.append(r.json()["id"])
    return pid, sid, shots[0], shots[1]


def test_shot_reference_delete_is_scoped_to_its_shot(api_env):
    client, tmp_path = api_env
    pid, _sid, shot_a, shot_b = _project_with_two_shots(client)
    cid = client.post(
        "/api/v1/characters",
        json={
            "name": "Hero",
            "appearance_prompt": "short black hair",
            "project_id": pid,
        },
    ).json()["id"]
    rel = _write_png(tmp_path, f"projects/{pid}/characters/{cid}/master.png")
    r = client.post(
        f"/api/v1/characters/{cid}/references",
        json={"image_path": rel, "role": "master", "is_primary": True},
    )
    assert r.status_code == 201, r.text
    char_ref_id = r.json()["id"]

    r = client.post(
        f"/api/v1/shots/{shot_a}/references",
        json={"character_id": cid, "purpose": "primary"},
    )
    assert r.status_code == 201, r.text
    shot_ref_id = r.json()["id"]

    # Another shot's route cannot delete it.
    r = client.delete(f"/api/v1/shots/{shot_b}/references/{shot_ref_id}")
    assert r.status_code == 404, r.text
    # A character-reference id is not a reference of this shot.
    r = client.delete(f"/api/v1/shots/{shot_a}/references/{char_ref_id}")
    assert r.status_code == 404, r.text
    # Unknown id.
    r = client.delete(f"/api/v1/shots/{shot_a}/references/{uuid.uuid4()}")
    assert r.status_code == 404, r.text
    assert [
        x["id"] for x in client.get(f"/api/v1/shots/{shot_a}/references").json()
    ] == [shot_ref_id]

    # Its own shot deletes it.
    r = client.delete(f"/api/v1/shots/{shot_a}/references/{shot_ref_id}")
    assert r.status_code == 204, r.text
    assert client.get(f"/api/v1/shots/{shot_a}/references").json() == []

    # The character reference is untouched and still deletable on its own route.
    r = client.get(f"/api/v1/characters/{cid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "master_thumbnail" in body
    assert "master_thumbnail_url" not in body
    assert body["master_thumbnail"] is not None
    r = client.delete(f"/api/v1/references/{char_ref_id}")
    assert r.status_code == 204, r.text


# --- 4. project card stats ------------------------------------------------------

CARD_FIELDS = (
    "thumbnail",
    "shot_count",
    "approved_shot_count",
    "clip_count",
    "monthly_cost_estimated_usd",
    "monthly_cost_actual_usd",
    "episode_count",
    "scene_count",
)


def test_project_card_stats(api_env):
    client, _tmp = api_env
    empty_pid = client.post("/api/v1/projects", json={"title": "empty"}).json()["id"]
    pid, _sid, shot_a, shot_b = _project_with_two_shots(client)

    # A batch (parent + 2 children): the parent must not double the cost.
    r = client.post(f"/api/v1/shots/{shot_a}/frame", json={"batch_size": 2})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    take = job["children"][0]["id"] if job.get("children") else job["id"]
    r = client.post(
        f"/api/v1/shots/{shot_a}/frame/approve", json={"generation_id": take}
    )
    assert r.status_code == 200, r.text

    from decimal import Decimal

    from app.models.generation import Generation

    with session_scope() as db:
        # Stub models cost $0: give the batch realistic costs (parent =
        # batch total, each child = unit price), all succeeded.
        for g in db.query(Generation).filter(Generation.shot_id == uuid.UUID(shot_a)).all():
            assert g.status == "succeeded", (g.id, g.status)
            cost = Decimal("2.00") if g.is_batch else Decimal("1.00")
            g.estimated_cost_usd = cost
            g.actual_cost_usd = cost
        for status in ("done", "failed"):
            db.add(
                Video(
                    shot_id=uuid.UUID(shot_a),
                    mode="i2v",
                    duration_sec=5.0,
                    fps=24,
                    resolution="1280x720",
                    model_id="stub",
                    output_path=f"v_{status}.mp4",
                    status=status,
                )
            )

    rows = {p["id"]: p for p in client.get("/api/v1/projects").json()}
    card = rows[pid]
    for field in CARD_FIELDS:
        assert field in card, field
    assert card["scene_count"] == 1
    assert card["shot_count"] == 2
    assert card["approved_shot_count"] == 1
    assert card["clip_count"] == 1  # only the done video
    assert isinstance(card["thumbnail"], str) and card["thumbnail"]
    assert card["monthly_cost_estimated_usd"] >= 0
    assert card["monthly_cost_actual_usd"] >= 0

    # The cost on the card equals the cost screen's current-month actual.
    summary = client.get(f"/api/v1/projects/{pid}/costs/summary").json()
    assert "total_actual_usd" in summary, summary
    assert card["monthly_cost_actual_usd"] == pytest.approx(2.0)  # children only
    assert card["monthly_cost_actual_usd"] == pytest.approx(
            summary["total_actual_usd"]
        )

    empty = rows[empty_pid]
    assert (empty["shot_count"], empty["approved_shot_count"], empty["clip_count"]) == (
        0,
        0,
        0,
    )
    assert empty["thumbnail"] is None

    # Detail returns the same card fields.
    detail = client.get(f"/api/v1/projects/{pid}").json()
    for field in CARD_FIELDS:
        assert detail[field] == card[field], field


# --- 5. SSE event contract ------------------------------------------------------


def _contract_events() -> dict[str, dict[str, str]]:
    return json.loads(
        (REPO / "packages" / "contracts" / "sse_events.json").read_text()
    )["events"]


def test_backend_sse_event_names_are_in_contract():
    pattern = re.compile(r"""["']((?:job|render|qc|cost)\.[a-z_]+)["']""")
    published: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        published.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert published, "no SSE event names found in app/"
    assert published <= set(_contract_events()), sorted(
        published - set(_contract_events())
    )


def test_cost_alert_contract_matches_budget_guard_payload():
    src = (APP_DIR / "services" / "budget_guard.py").read_text(encoding="utf-8")
    m = re.search(r'"cost\.alert",\s*\{(.*?)\}', src, re.S)
    assert m, "cost.alert publish not found in budget_guard.py"
    published_keys = set(re.findall(r'"([a-z_]+)":', m.group(1)))
    contract_keys = {k.rstrip("?") for k in _contract_events()["cost.alert"]}
    assert published_keys == contract_keys


def _list_query_count(client) -> int:
    from sqlalchemy import event

    engine = sess_mod.get_engine()
    statements: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        r = client.get("/api/v1/projects")
        assert r.status_code == 200, r.text
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return len(statements)


def test_project_list_query_count_does_not_grow_with_projects(api_env):
    """Review B6-M2: the list is a fixed number of queries (card stats are
    grouped, pack names come from one query), not one per project."""
    client, _tmp = api_env
    packs = client.get("/api/v1/category-packs").json()
    code = packs[0]["code"]

    client.post("/api/v1/projects", json={"title": "p0", "category_pack_code": code})
    one = _list_query_count(client)
    for i in range(1, 8):
        r = client.post("/api/v1/projects", json={"title": f"p{i}", "category_pack_code": code})
        assert r.status_code == 201, r.text
    eight = _list_query_count(client)
    assert len(client.get("/api/v1/projects").json()) == 8
    assert eight == one, (one, eight)
