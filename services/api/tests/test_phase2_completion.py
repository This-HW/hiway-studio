"""Phase-2 completion proof (stub adapters, no API keys).

Proves the Phase-2 acceptance criteria end to end:
  1. scene/shot CRUD + storyboard ordering (first shot gets the establishing
     prefill),
  2. frame batch generation (parent + 2 children) for 5 shots, with files at
     projects/{pid}/scenes/{sid}/shots/{shot}/frames/,
  3. §7 prompt assembly (ADR-0011 §1 slots): subject/setting/style_medium/
     lighting/camera_lens/color_mood/params + reference priority cap,
  4. inpaint as an additional take,
  5. approve a batch child (parent id and foreign takes rejected),
  6. continuity fallback chain: shot 2 sees shot 1's approved still; shot 1
     (first) reports unavailable,
  7. Idempotency-Key replays the original 202 without new jobs,
  8. the Phase-3 approval gate: require_approved_frame raises 409
     APPROVAL_REQUIRED for unapproved shots,
  9. prompt truncation never cuts subject/lighting/color_mood cores;
     absurdly long subject raises PROMPT_TOO_LONG,
 10. GET /jobs filters.

Celery runs eager (task_always_eager), so 202 responses complete inline.
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.generation import Generation
from app.models.production import Shot
from app.services import approval as approval_service
from app.services import prompt_assembly
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
from app.tasks.celery_app import celery_app

import app.main  # noqa: E402

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

    seed_dir = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "category_packs"
    with session_scope() as db:
        assert seed_category_packs(db, seed_dir) == 3

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


def test_phase2_completion(api_env):
    client, tmp_path = api_env

    # --- project / scene / shots -----------------------------------------
    r = client.post("/api/v1/projects", json={"title": "P2 proof"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = client.post(
        f"/api/v1/projects/{pid}/scenes",
        json={
            "title": "Opening",
            "location": "Rooftop garden",
            "time_of_day": "Dusk",
            "lighting": "neon glow",
        },
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    shot_ids: list[str] = []
    for i in range(5):
        r = client.post(
            f"/api/v1/scenes/{sid}/shots",
            json={"action": f"Hero looks over the city, beat {i}", "duration_sec": 5},
        )
        assert r.status_code == 201, r.text
        shot_ids.append(r.json()["id"])
    shot0, shot1, shot2 = shot_ids[0], shot_ids[1], shot_ids[2]

    # First shot gets the establishing prefill; order is sequential.
    r = client.get(f"/api/v1/shots/{shot0}")
    assert r.json()["shot_type"] == "establishing"
    assert [s["order_index"] for s in client.get(f"/api/v1/scenes/{sid}/shots").json()] == [0, 1, 2, 3, 4]

    # --- character + references ------------------------------------------
    r = client.post(
        "/api/v1/characters",
        json={
            "name": "Hero",
            "appearance_prompt": "a young woman with short black hair and a scar on her left cheek",
            "project_id": pid,
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    master_rel = _write_png(tmp_path, f"projects/{pid}/characters/{cid}/master.png")
    r = client.post(
        f"/api/v1/characters/{cid}/references",
        json={"image_path": master_rel, "role": "master", "is_primary": True},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        f"/api/v1/shots/{shot0}/references",
        json={"character_id": cid, "purpose": "primary"},
    )
    assert r.status_code == 201, r.text

    r = client.post(
        f"/api/v1/characters/{cid}/wardrobe",
        json={"name": "street", "prompt_fragment": "oversized denim jacket", "is_default": True},
    )
    assert r.status_code == 201, r.text

    # --- frame batches for all 5 shots ------------------------------------
    batch_ids: list[str] = []
    for s in shot_ids:
        r = client.post(f"/api/v1/shots/{s}/frame", json={"batch_size": 2})
        assert r.status_code == 202, r.text
        body = r.json()
        assert "job_id" in body and "estimated_cost_usd" in body
        batch_ids.append(body["job_id"])

    for s in shot_ids:
        r = client.get(f"/api/v1/shots/{s}")
        card = r.json()
        assert card["status"] == "frame_done", card
        assert card["candidate_count"] == 2
        assert card["thumbnail"] is not None

    frames_dir = tmp_path / "projects" / pid / "scenes" / sid / "shots" / shot0 / "frames"
    assert len(list(frames_dir.glob("*.png"))) == 2
    assert len(list(frames_dir.glob("*.thumb.jpg"))) == 2

    # §7 prompt assembly is visible on the job row.
    r = client.get("/api/v1/jobs", params={"shot_id": shot0, "job_type": "text_to_image"})
    assert r.status_code == 200
    child = next(j for j in r.json() if not j["is_batch"])
    prompt = child["prompt_final"]
    assert "short black hair" in prompt  # appearance
    assert "oversized denim jacket" in prompt  # wardrobe
    assert "Rooftop garden" in prompt  # scene_context
    assert child["reference_ids"], "reference snapshot must be recorded"

    # --- approval ----------------------------------------------------------
    r = client.get(f"/api/v1/jobs/{batch_ids[0]}")
    take0 = r.json()["children"][0]["id"]
    r = client.post(f"/api/v1/shots/{shot0}/frame/approve", json={"generation_id": take0})
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "frame_approved"
    assert approved["approved_frame_generation_id"] == take0

    # Approving the batch parent is rejected (D-26).
    r = client.post(
        f"/api/v1/shots/{shot0}/frame/approve", json={"generation_id": batch_ids[0]}
    )
    assert r.status_code == 400, r.text

    # Approving a take from another shot is rejected.
    foreign = client.get(f"/api/v1/jobs/{batch_ids[1]}").json()["children"][0]["id"]
    r = client.post(f"/api/v1/shots/{shot0}/frame/approve", json={"generation_id": foreign})
    assert r.status_code == 400, r.text

    # --- inpaint as an extra take (ADR-0011 §10: approved take only) --------
    mask_rel = _write_png(tmp_path, f"projects/{pid}/masks/m1.png")
    r = client.post(
        f"/api/v1/shots/{shot0}/inpaint",
        json={"generation_id": take0, "mask_path": mask_rel, "prompt": "remove the pigeon"},
    )
    assert r.status_code == 202, r.text
    r = client.get(f"/api/v1/shots/{shot0}")
    assert r.json()["candidate_count"] == 3  # 2 batch takes + inpaint

    # Inpaint on an unapproved take is rejected (ADR-0011 §10).
    batch0 = client.get(f"/api/v1/jobs/{batch_ids[0]}").json()
    take1 = batch0["children"][1]["id"]  # second child, not approved
    r = client.post(
        f"/api/v1/shots/{shot0}/inpaint",
        json={"generation_id": take1, "mask_path": mask_rel, "prompt": "x"},
    )
    assert r.status_code == 409, r.text

    # --- continuity (D-15) --------------------------------------------------
    r = client.get(f"/api/v1/shots/{shot1}/continuity")
    assert r.status_code == 200, r.text
    cont = r.json()
    assert cont["available"] is True
    assert cont["source_shot_id"] == shot0
    assert "Rooftop garden" in cont["summary"]

    r = client.post(f"/api/v1/shots/{shot1}/frame", json={"use_continuity": True})
    assert r.status_code == 202, r.text
    cont_job_id = r.json()["job_id"]
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(cont_job_id))
        assert gen.params["continuity"]["used"] is True
        assert "Continuity:" in gen.prompt_final

    # First shot has no previous shot: unavailable with a reason.
    r = client.get(f"/api/v1/shots/{shot0}/continuity")
    assert r.json()["available"] is False and r.json()["reason"]

    # --- idempotency --------------------------------------------------------
    headers = {"Idempotency-Key": "p2-proof-key-1"}
    r1 = client.post(f"/api/v1/shots/{shot2}/frame", json={"batch_size": 1}, headers=headers)
    r2 = client.post(f"/api/v1/shots/{shot2}/frame", json={"batch_size": 1}, headers=headers)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]
    r = client.get("/api/v1/jobs", params={"shot_id": shot2})
    # Earlier batch (parent + 2 children) + one idempotent batch (parent + child);
    # the replay created nothing.
    assert len(r.json()) == 5

    # --- Phase-3 approval gate (boundary function) --------------------------
    with session_scope() as db:
        unapproved = db.get(Shot, uuid.UUID(shot2))
        with pytest.raises(StudioError) as exc_info:
            approval_service.require_approved_frame(db, unapproved)
        assert exc_info.value.code == ErrorCode.APPROVAL_REQUIRED
        from app.core.errors import ERROR_HTTP_STATUS

        assert ERROR_HTTP_STATUS[exc_info.value.code] == 409
        approved_shot = db.get(Shot, uuid.UUID(shot0))
        gen = approval_service.require_approved_frame(db, approved_shot)
        assert str(gen.id) == take0

    # --- prompt truncation (§7.1 #11) ---------------------------------------
    with session_scope() as db:
        shot_uuid = uuid.UUID(shot1)
        assembled = prompt_assembly.assemble_shot_prompt(
            db, shot_uuid, overrides={"setting": "x" * 1500, "subject": "y" * 900}
        )
        assert len(assembled["prompt_final"]) <= 2000
        assert "setting" in assembled["truncated_sections"]
        # Subject / lighting / color_mood cores are never truncated.
        assert "y" * 900 in assembled["prompt_final"]

        # Absurdly long appearance: nothing truncatable -> PROMPT_TOO_LONG.
        r = client.post(
            "/api/v1/characters",
            json={
                "name": "Verbose",
                "appearance_prompt": "z" * 2500,
                "project_id": pid,
            },
        )
        verbose_cid = r.json()["id"]
        r = client.post(
            f"/api/v1/shots/{shot2}/references",
            json={"character_id": verbose_cid, "purpose": "primary"},
        )
        with pytest.raises(StudioError) as exc_info:
            prompt_assembly.assemble_shot_prompt(db, uuid.UUID(shot2))
        assert exc_info.value.code == ErrorCode.PROMPT_TOO_LONG

    # --- GET /jobs filters ---------------------------------------------------
    r = client.get("/api/v1/jobs", params={"status": "succeeded"})
    assert r.status_code == 200 and len(r.json()) > 0
    r = client.get("/api/v1/jobs", params={"job_type": "inpaint", "shot_id": shot0})
    assert len(r.json()) == 1 and r.json()[0]["status"] == "succeeded"


def test_reference_priority_cap(api_env):
    """§7.2: at most 4 references, primary > expression > outfit > location > continuity."""
    client, tmp_path = api_env
    pid = client.post("/api/v1/projects", json={"title": "refcap"}).json()["id"]
    sid = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "S"}).json()["id"]
    r = client.post(
        f"/api/v1/scenes/{sid}/shots",
        json={"dialogue": "Hello!", "emotion_weights": {"happy": 0.9}},
    )
    assert r.status_code == 201, r.text
    shot_id = r.json()["id"]
    cid = client.post(
        "/api/v1/characters",
        json={"name": "Ref", "appearance_prompt": "plain face", "project_id": pid},
    ).json()["id"]

    role_to_id: dict[str, str] = {}
    for role in ("master", "expression_happy", "outfit_default"):
        rel = _write_png(tmp_path, f"projects/{pid}/characters/{cid}/{role}.png")
        r = client.post(
            f"/api/v1/characters/{cid}/references",
            json={"image_path": rel, "role": role, "is_primary": role == "master"},
        )
        assert r.status_code == 201, r.text
        role_to_id[role] = r.json()["id"]

    client.post(
        f"/api/v1/shots/{shot_id}/references",
        json={"character_id": cid, "purpose": "primary"},
    )

    # Location asset linked to the shot.
    r = client.post(
        "/api/v1/assets/upload",
        files={"file": ("loc.png", _TINY_PNG, "image/png")},
        data={"kind": "location", "project_id": pid},
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["asset_id"]
    r = client.post(
        f"/api/v1/shots/{shot_id}/references",
        json={"asset_id": asset_id, "purpose": "location"},
    )
    assert r.status_code == 201, r.text

    with session_scope() as db:
        # Five eligible references (primary, expression, outfit, location,
        # continuity) with a cap of 4.
        assembled = prompt_assembly.assemble_shot_prompt(
            db,
            uuid.UUID(shot_id),
            continuity_summary="previous state",
            continuity_image_path="projects/x/prev.png",
            continuity_source_id="prev-gen-id",
        )
        kept = assembled["reference_ids"]
        assert len(kept) == 4, kept
        # Priority order: primary first; continuity drops out.
        assert kept[0] == role_to_id["master"]
        assert role_to_id["expression_happy"] in kept
        assert role_to_id["outfit_default"] in kept
        assert asset_id in kept
        assert "prev-gen-id" not in kept


def test_queue_full_429(api_env):
    client, tmp_path = api_env
    pid = client.post("/api/v1/projects", json={"title": "qfull"}).json()["id"]
    sid = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "S"}).json()["id"]
    shot_id = client.post(f"/api/v1/scenes/{sid}/shots", json={}).json()["id"]

    with session_scope() as db:
        for _ in range(101):
            db.add(
                Generation(
                    project_id=uuid.UUID(pid),
                    job_type="text_to_image",
                    status="queued",
                    model_id="stub.image",
                )
            )
        db.flush()

    r = client.post(f"/api/v1/shots/{shot_id}/frame", json={})
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "QUEUE_FULL"
