"""Phase-5B completion proof (P1-1 multishot, P1-4 LoRA, P1-7 style extract,
P1-9 thumbnails).

  1. POST /shots/{id}/multishot -> 202 {multishot_set_id, job_id}: creates the
     set + 6 MultishotFrame rows (all coverage angles). The q.p1 worker
     (run_multishot) inserts 6 angle shots right after the base shot, each
     with one frame generation; on_frame_finished aggregates to done.
  2. Base shot without an approved frame -> 409 APPROVAL_REQUIRED.
  3. POST /characters/{id}/lora/train with <8 images -> 422
     LORA_TOO_FEW_IMAGES; with confirm=true -> 202. complete_train writes
     the stub weight file and links the character (consistency_method=lora).
  4. POST /style-kits/{id}/extract: <3 sources -> 202 + warning;
     >8 sources -> 202 with images_used == 8. complete_extract writes the
     stub style fields and bumps the kit version.
  5. POST /projects/{id}/thumbnails -> 202 parent + N children; the PIL
     text overlay (render_thumbnail) produces a real PNG with the title band.

The test mounts a fresh FastAPI app with only the new routers so it is
independent of main.py router registration (the parent registers them at
the end of Phase 5). Celery dispatchers are stubbed with no-ops; workers
are invoked directly as service calls.
"""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.celery_app import register_job_dispatcher
from app.core.config import get_settings
from app.core.middleware import register_middleware
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.asset import Asset
from app.models.character import Character, CharacterReference
from app.models.generation import Generation
from app.models.lora import LoraModel
from app.models.multishot import COVERAGE_ANGLES, MultishotFrame, MultishotSet
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.style import StyleKit
from app.routers import characters as characters_router
from app.routers import multishots as multishots_router
from app.routers import style_kits as style_kits_router
from app.routers import thumbnails as thumbnails_router


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

    # Model registry seeds (lora/extract/thumbnail cost lookups need them).
    from app.models_lib.seed import apply_seed as apply_model_seed
    apply_model_seed()

    # Never touch celery/redis in tests: swallow all P1-B dispatches.
    for task_name in (
        "q.p1.run_multishot",
        "q.p1.run_lora_train",
        "q.p1.run_style_extract",
        "q.image.run_generation",
    ):
        register_job_dispatcher(task_name, lambda *a: None)

    api = FastAPI()
    register_middleware(api)
    api.include_router(multishots_router.router, prefix="/api/v1")
    api.include_router(thumbnails_router.router, prefix="/api/v1")
    api.include_router(characters_router.router, prefix="/api/v1")
    api.include_router(style_kits_router.router, prefix="/api/v1")
    c = TestClient(api)
    yield c
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _project() -> uuid.UUID:
    with session_scope() as db:
        p = Project(id=uuid.uuid4(), title="P5B", aspect_ratio="16:9")
        db.add(p)
        db.flush()
        return p.id


def _approved_shot(project_id: uuid.UUID) -> uuid.UUID:
    """Scene + shot with an approved frame (multishot base)."""
    with session_scope() as db:
        scene = Scene(id=uuid.uuid4(), project_id=project_id, order_index=0, title="S1")
        db.add(scene)
        db.flush()
        shot = Shot(
            id=uuid.uuid4(), scene_id=scene.id, order_index=0,
            shot_type="wide", duration_sec=5.0, status="frame_approved",
        )
        db.add(shot)
        db.flush()
        gen = Generation(
            id=uuid.uuid4(), project_id=project_id, job_type="text_to_image",
            shot_id=shot.id, status="succeeded", model_id="stub-image",
            output_path="projects/x/frames/base.png",
        )
        db.add(gen)
        db.flush()
        shot.approved_frame_generation_id = gen.id
        db.flush()
        return shot.id


def _character_with_refs(project_id: uuid.UUID, n_refs: int) -> uuid.UUID:
    with session_scope() as db:
        ch = Character(
            id=uuid.uuid4(), project_id=project_id, name="Hero",
            appearance_prompt="a brave knight",
        )
        db.add(ch)
        db.flush()
        for i in range(n_refs):
            db.add(CharacterReference(
                id=uuid.uuid4(), character_id=ch.id,
                image_path=f"projects/x/refs/r{i}.png", role="training",
                source="uploaded",
            ))
        db.flush()
        return ch.id


def _kit_with_assets(project_id: uuid.UUID, n_assets: int) -> uuid.UUID:
    with session_scope() as db:
        kit = StyleKit(id=uuid.uuid4(), project_id=project_id, name="Kit")
        db.add(kit)
        db.flush()
        for i in range(n_assets):
            db.add(Asset(
                id=uuid.uuid4(), project_id=project_id, kind="image",
                name=f"src{i}", image_path=f"projects/x/assets/s{i}.png",
            ))
        db.flush()
        return kit.id


def _asset_ids(project_id: uuid.UUID) -> list[uuid.UUID]:
    with session_scope() as db:
        return [a.id for a in db.query(Asset).filter(
            Asset.project_id == project_id).all()]


# --- P1-1: multishot ----------------------------------------------------------


def test_multishot_request_and_worker(client):
    from app.services import multishot as multishot_service

    project_id = _project()
    shot_id = _approved_shot(project_id)

    r = client.post(f"/api/v1/shots/{shot_id}/multishot", json={})
    assert r.status_code == 202, r.text
    body = r.json()
    set_id = body["multishot_set_id"]
    assert body["job_id"] == set_id

    # The request locked 6 pending frames, one per coverage angle.
    with session_scope() as db:
        frames = db.query(MultishotFrame).filter(
            MultishotFrame.multishot_set_id == uuid.UUID(set_id)).all()
        assert {f.coverage_type for f in frames} == set(COVERAGE_ANGLES)
        assert all(f.status == "pending" for f in frames)

    # Run the worker inline: 6 angle shots land right after the base shot.
    with session_scope() as db:
        summary = multishot_service.run_multishot(db, set_id)
        assert summary["status"] == "running"
        assert len(summary["job_ids"]) == 6
    with session_scope() as db:
        scene_id = db.query(Shot.scene_id).filter(Shot.id == shot_id).scalar()
        shots = db.query(Shot).filter(Shot.scene_id == scene_id).order_by(
            Shot.order_index).all()
        assert [s.order_index for s in shots] == list(range(7))
        coverage = [s for s in shots if s.id != shot_id]
        assert all(s.coverage_source_frame_id is not None for s in coverage)
        gens = db.query(Generation).filter(
            Generation.job_type == "text_to_image",
            Generation.shot_id.in_([s.id for s in coverage])).all()
        assert len(gens) == 6

    # Aggregation: all frames done -> set done.
    with session_scope() as db:
        frames = db.query(MultishotFrame).filter(
            MultishotFrame.multishot_set_id == uuid.UUID(set_id)).all()
        for f in frames:
            multishot_service.on_frame_finished(db, str(f.id), True)
        mset = db.get(MultishotSet, uuid.UUID(set_id))
        assert mset.status == "done"

    # Read-back endpoint.
    r = client.get(f"/api/v1/multishot-sets/{set_id}")
    assert r.status_code == 200, r.text
    assert len(r.json()["frames"]) == 6


def test_multishot_requires_approved_frame(client):
    project_id = _project()
    with session_scope() as db:
        scene = Scene(id=uuid.uuid4(), project_id=project_id, order_index=0)
        db.add(scene)
        db.flush()
        shot = Shot(id=uuid.uuid4(), scene_id=scene.id, order_index=0,
                    shot_type="wide", duration_sec=5.0, status="draft")
        db.add(shot)
        db.flush()
        shot_id = shot.id
    r = client.post(f"/api/v1/shots/{shot_id}/multishot", json={})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "APPROVAL_REQUIRED"


# --- P1-4: LoRA ---------------------------------------------------------------


def test_lora_train_few_images_guard_and_confirm(client):
    from app.services import lora as lora_service

    project_id = _project()
    char_id = _character_with_refs(project_id, 2)
    with session_scope() as db:
        ref_ids = [str(r.id) for r in db.query(CharacterReference).filter(
            CharacterReference.character_id == char_id).all()]

    r = client.post(
        f"/api/v1/characters/{char_id}/lora/train",
        json={"training_image_ids": ref_ids, "trigger_word": "heroink"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "LORA_TOO_FEW_IMAGES"

    r = client.post(
        f"/api/v1/characters/{char_id}/lora/train",
        json={"training_image_ids": ref_ids, "trigger_word": "heroink",
              "confirm": True, "rank": 16, "steps": 500},
    )
    assert r.status_code == 202, r.text
    lora_id = r.json()["lora_model_id"]

    with session_scope() as db:
        lora = db.get(LoraModel, uuid.UUID(lora_id))
        assert lora.status == "training"
        assert lora.training_image_count == 2
        # The worker completes the stub training synchronously here.
        result = lora_service.complete_train(db, lora_id)
        assert result["status"] == "ready"
        assert result["model_path"].endswith("heroink.safetensors")
        ch = db.get(Character, char_id)
        assert ch.consistency_method == "lora"


# --- P1-7: style extract ------------------------------------------------------


def test_style_extract_count_rules(client):
    from app.services import style_extract as extract_service

    project_id = _project()
    kit_id = _kit_with_assets(project_id, 2)
    aids = [str(a) for a in _asset_ids(project_id)]

    r = client.post(f"/api/v1/style-kits/{kit_id}/extract",
                    json={"source_asset_ids": aids})
    assert r.status_code == 202, r.text
    assert r.json()["warning"] is not None  # <3 sources: warn but proceed
    assert r.json()["images_used"] == 2

    kit2_id = _kit_with_assets(project_id, 10)
    aids2 = [str(a) for a in _asset_ids(project_id)
             if a not in {uuid.UUID(x) for x in aids}][:10]
    r = client.post(f"/api/v1/style-kits/{kit2_id}/extract",
                    json={"source_asset_ids": aids2})
    assert r.status_code == 202, r.text
    assert r.json()["images_used"] == 8  # >8: first 8 only

    with session_scope() as db:
        result = extract_service.complete_extract(db, str(kit_id))
        assert result["status"] == "succeeded"
        kit = db.get(StyleKit, kit_id)
        assert kit.color is not None and kit.version == 2


# --- P1-9: thumbnails ---------------------------------------------------------


def test_thumbnails_batch_and_overlay(client):
    from app.media.thumbnail_text import render_thumbnail
    from PIL import Image

    project_id = _project()
    r = client.post(
        f"/api/v1/projects/{project_id}/thumbnails",
        json={"title_text": "EP1 하이라이트", "count": 3},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["count"] == 3
    with session_scope() as db:
        children = db.query(Generation).filter(
            Generation.parent_generation_id == uuid.UUID(body["job_id"])).all()
        assert len(children) == 3
        assert all("text" in (c.negative_final or "").lower()
                   or "letters" in (c.negative_final or "").lower()
                   for c in children)

    # The §10.5 overlay: title text is composited by PIL, never by the model.
    src = Image.new("RGB", (640, 360), (30, 30, 30))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    out = render_thumbnail(buf.getvalue(), "EP1 하이라이트")
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(out) > 1000
    img = Image.open(io.BytesIO(out))
    assert img.size == (1280, 720)


def test_thumbnails_count_bounds(client):
    project_id = _project()
    # Schema-level bound (Field ge=2 le=8) rejects before the service runs.
    r = client.post(f"/api/v1/projects/{project_id}/thumbnails",
                    json={"count": 9})
    assert r.status_code == 400, r.text


def test_thumbnails_idempotency_replay(client):
    """Idempotency-Key replay on thumbnails must not 500 (NameError regression)."""
    project_id = _project()
    key = "thumb-replay-1"
    r1 = client.post(
        f"/api/v1/projects/{project_id}/thumbnails",
        json={"title_text": "replay", "count": 2},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 202, r1.text
    r2 = client.post(
        f"/api/v1/projects/{project_id}/thumbnails",
        json={"title_text": "replay", "count": 2},
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 202, r2.text
    assert r2.json()["job_id"] == r1.json()["job_id"]
