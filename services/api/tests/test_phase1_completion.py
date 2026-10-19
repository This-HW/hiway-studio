"""Phase-1 completion proof (stub adapters, no API keys).

Proves the Phase-1 acceptance criteria end to end:
  1. project CRUD + archive/duplicate,
  2. story: sync stub-LLM synopsis (201) and beats (201), beat CRUD, script import,
  3. character: create -> master portrait (202 job) -> CharacterReference row +
     file at projects/{pid}/characters/{cid}/master.png,
  4. character sheet: turnaround + expression jobs -> reference rows,
  5. global character reused as a shot reference and visible via scope=all,
  6. style kit: create/lock (locked update -> 409)/duplicate,
  7. category packs: 3 built-ins seeded,
  8. assets: upload/list/delete, global + project scopes,
  9. thumbnails: a .thumb.jpg is written next to the master portrait.

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
from app.models.production import Scene, Shot, ShotReference
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401 (registers the q.image dispatcher)
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

    celery_app.conf.task_always_eager = True
    client = TestClient(app.main.app)
    yield client, tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def test_phase1_completion(api_env):
    client, tmp_path = api_env

    # --- projects ---------------------------------------------------------
    r = client.post("/api/v1/projects", json={"title": "P1 proof"})
    assert r.status_code == 201, r.text
    project = r.json()
    pid = project["id"]
    assert (tmp_path / "projects" / pid / "project.json").exists()

    r = client.get("/api/v1/category-packs")
    assert r.status_code == 200
    assert {p["code"] for p in r.json()} == {"knowledge_info", "drama_shortform", "ai_influencer_daily"}

    r = client.get("/api/v1/models", params={"modality": "image"})
    assert r.status_code == 200 and len(r.json()) >= 1

    # --- story: sync stub LLM ---------------------------------------------
    r = client.post(
        f"/api/v1/projects/{pid}/story/generate-synopsis",
        json={"logline": "A robot learns to farm."},
    )
    assert r.status_code == 201, r.text
    assert r.json()["llm_call_id"]
    assert "robot" in r.json()["synopsis"].lower() or "stub" in r.json()["synopsis"].lower()

    r = client.post(f"/api/v1/projects/{pid}/story/generate-beats", json={})
    assert r.status_code == 201, r.text
    beats = r.json()["beats"]
    assert len(beats) == 3
    story_id = beats[0]["story_id"]

    r = client.get(f"/api/v1/stories/{story_id}/beats")
    assert r.status_code == 200 and len(r.json()) == 3

    r = client.post(f"/api/v1/stories/{story_id}/beats", json={"title": "Extra", "summary": "x"})
    assert r.status_code == 201
    extra_id = r.json()["id"]
    r = client.patch(f"/api/v1/beats/{extra_id}", json={"order_index": 0})
    assert r.status_code == 200 and r.json()["order_index"] == 0
    r = client.delete(f"/api/v1/beats/{extra_id}")
    assert r.status_code == 204
    r = client.get(f"/api/v1/stories/{story_id}/beats")
    assert [b["order_index"] for b in r.json()] == [0, 1, 2]  # reindexed after delete

    script = "## Scene: Greenhouse\nLocation: Farm\nTime: Dawn\nMood: Hopeful\n"
    r = client.post(
        f"/api/v1/projects/{pid}/story/import",
        files={"file": ("script.md", script.encode(), "text/markdown")},
    )
    assert r.status_code == 201 and r.json()["scenes_created"] == 1

    # --- character: master portrait ---------------------------------------
    r = client.post(
        f"/api/v1/projects/{pid}/characters",
        json={"name": "R-0", "appearance_prompt": "a friendly farming robot, orange paint"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = client.post(f"/api/v1/characters/{cid}/master", json={})
    assert r.status_code == 202, r.text

    r = client.get(f"/api/v1/characters/{cid}/references")
    assert r.status_code == 200
    refs = r.json()
    masters = [x for x in refs if x["role"] == "master"]
    assert len(masters) == 1 and masters[0]["is_primary"] is True
    master_path = tmp_path / masters[0]["image_path"]
    assert master_path.exists(), masters[0]["image_path"]
    assert not masters[0]["image_path"].startswith("/")  # relative path in DB
    assert (tmp_path / "projects" / pid / "characters" / cid / "master.png").exists()
    assert master_path.with_name(master_path.stem + ".thumb.jpg").exists()  # thumbnail

    # --- static file serving (design §5: GET /files/...) ---------------------
    r = client.get(f"/files/{masters[0]['image_path']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    assert client.get("/files/../app/main.py").status_code == 404  # no traversal
    assert client.get("/files/does/not/exist.png").status_code == 404

    # --- character sheet ---------------------------------------------------
    r = client.post(
        f"/api/v1/characters/{cid}/sheet",
        json={"views": ["turnaround_front", "turnaround_profile"], "expressions": ["happy"]},
    )
    assert r.status_code == 202, r.text
    assert len(r.json()["job_ids"]) == 3

    r = client.get(f"/api/v1/characters/{cid}/references")
    roles = {x["role"] for x in r.json()}
    assert {"master", "turnaround_front", "turnaround_profile", "expression_happy"} <= roles

    # --- global character reused as a shot reference -----------------------
    r = client.post(
        "/api/v1/characters",
        json={"name": "Libby", "appearance_prompt": "a librarian ghost"},
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    r = client.get(f"/api/v1/projects/{pid}/characters", params={"scope": "all"})
    assert r.status_code == 200
    assert {c["id"] for c in r.json()} == {cid, gid}
    r = client.get(f"/api/v1/projects/{pid}/characters", params={"scope": "global"})
    assert [c["id"] for c in r.json()] == [gid]

    with session_scope() as db:
        scene = Scene(id=uuid.uuid4(), project_id=uuid.UUID(pid), order_index=0, title="S1")
        db.add(scene)
        db.flush()
        shot = Shot(id=uuid.uuid4(), scene_id=scene.id, order_index=0, duration_sec=5.0)
        db.add(shot)
        db.flush()
        db.add(
            ShotReference(
                id=uuid.uuid4(),
                shot_id=shot.id,
                character_id=uuid.UUID(gid),  # global character, project shot
                purpose="cast",
            )
        )

    # --- style kits ---------------------------------------------------------
    r = client.post(f"/api/v1/projects/{pid}/style-kits", json={"name": "Warm doc", "color": "warm"})
    assert r.status_code == 201, r.text
    kit_id = r.json()["id"]
    r = client.post(f"/api/v1/style-kits/{kit_id}/lock")
    assert r.status_code == 200 and r.json()["is_locked"] is True
    r = client.patch(f"/api/v1/style-kits/{kit_id}", json={"color": "cold"})
    assert r.status_code == 409  # locked kits are immutable
    r = client.post(f"/api/v1/style-kits/{kit_id}/duplicate")
    assert r.status_code == 201
    dup_id = r.json()["id"]
    assert r.json()["is_locked"] is False
    r = client.patch(f"/api/v1/style-kits/{dup_id}", json={"color": "cold"})
    assert r.status_code == 200 and r.json()["color"] == "cold"

    # --- assets --------------------------------------------------------------
    r = client.post(
        "/api/v1/assets/upload",
        params={"kind": "background", "name": "Greenhouse BG"},
        files={"file": ("bg.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    )
    assert r.status_code == 201, r.text
    asset_id = r.json()["asset_id"]
    r = client.get(f"/api/v1/projects/{pid}/assets")
    assert r.status_code == 200 and any(a["id"] == asset_id for a in r.json())
    r = client.delete(f"/api/v1/assets/{asset_id}")
    assert r.status_code == 204

    # --- master regeneration: new file, primary demoted (ADR-0005) ----------
    r = client.post(f"/api/v1/characters/{cid}/master", json={})
    assert r.status_code == 202, r.text
    r = client.get(f"/api/v1/characters/{cid}/references")
    masters = [x for x in r.json() if x["role"] == "master"]
    assert len(masters) == 2
    from pathlib import PurePosixPath

    primaries = [x for x in masters if x["is_primary"]]
    assert len(primaries) == 1
    new_name = PurePosixPath(primaries[0]["image_path"]).name
    assert new_name.startswith("master_") and new_name.endswith(".png")  # new file
    old = [x for x in masters if not x["is_primary"]][0]
    assert PurePosixPath(old["image_path"]).name == "master.png"  # first master kept
    assert (tmp_path / primaries[0]["image_path"]).exists()  # new file, not overwrite
    assert (tmp_path / old["image_path"]).exists()  # old file kept (immutable)

    # --- episode clone: deep copy of scenes/shots/references ----------------
    with session_scope() as db:
        from app.models.story import Episode
        from app.services.episode_clone import clone_episode

        ep = Episode(id=uuid.uuid4(), project_id=uuid.UUID(pid), order_index=0, title="E1")
        db.add(ep)
        db.flush()
        ep_id = ep.id
        sc = Scene(id=uuid.uuid4(), project_id=uuid.UUID(pid), episode_id=ep_id,
                   order_index=0, title="CloneMe")
        db.add(sc)
        db.flush()
        sh = Shot(id=uuid.uuid4(), scene_id=sc.id, order_index=0, duration_sec=5.0,
                  status="approved", approved_at=None)
        db.add(sh)
        db.flush()
        sh_id = sh.id
        db.add(ShotReference(id=uuid.uuid4(), shot_id=sh_id,
                             character_id=uuid.UUID(gid), purpose="cast"))
        db.flush()
        clone = clone_episode(db, ep_id)
        assert clone.id != ep_id and clone.status == "draft"
        cloned_scenes = db.query(Scene).filter(Scene.episode_id == clone.id).all()
        assert len(cloned_scenes) == 1 and cloned_scenes[0].id != sc.id
        cloned_shots = db.query(Shot).filter(Shot.scene_id == cloned_scenes[0].id).all()
        assert len(cloned_shots) == 1
        assert cloned_shots[0].id != sh_id
        assert cloned_shots[0].status == "draft"  # reset, regenerates from draft
        assert cloned_shots[0].approved_frame_generation_id is None
        cloned_refs = db.query(ShotReference).filter(
            ShotReference.shot_id == cloned_shots[0].id).all()
        assert len(cloned_refs) == 1
        assert cloned_refs[0].character_id == uuid.UUID(gid)  # binding kept

    # --- duplicate + archive --------------------------------------------------
    r = client.post(f"/api/v1/projects/{pid}/duplicate")
    assert r.status_code == 201, r.text
    dup_pid = r.json()["id"]
    assert (tmp_path / "projects" / dup_pid / "characters" / cid).exists() is False  # new ids
    dup_chars = list((tmp_path / "projects" / dup_pid / "characters").iterdir())
    assert len(dup_chars) == 1  # character dir copied under a new id

    r = client.post(f"/api/v1/projects/{pid}/archive")
    assert r.status_code == 200
    r = client.get("/api/v1/projects")
    assert all(p["id"] != pid for p in r.json())
    r = client.get("/api/v1/projects", params={"is_archived": "true"})
    assert any(p["id"] == pid for p in r.json())
