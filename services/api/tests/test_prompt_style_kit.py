"""Regression: 7-slot prompt assembly with a style kit attached.

`_slot_style_medium` once referenced `StyleKit.medium`, which does not exist
on the model (fields are color/lighting/texture/composition/lens/...), so any
shot resolving a style kit crashed with AttributeError. Existing tests only
assembled prompts without a kit, so they never hit the line.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
import app.main  # noqa: E402
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.services import prompt_assembly
from app.services.style_kit import seed_category_packs
from app.tasks import tasks_image  # noqa: F401
from app.tasks.celery_app import celery_app


def _api_env(tmp_path, monkeypatch):
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
    return TestClient(app.main.app)


def test_assemble_with_style_kit_no_attribute_error(tmp_path, monkeypatch):
    client = _api_env(tmp_path, monkeypatch)
    try:
        p = client.post("/api/v1/projects", json={"title": "kit-regression"}).json()
        kit = client.post(
            f"/api/v1/projects/{p['id']}/style-kits",
            json={
                "name": "Test kit",
                "texture": "35mm film grain, photographic",
                "composition": "rule of thirds",
                "color": "teal and orange",
                "lighting": "soft window light",
                "lens": "35mm lens",
            },
        ).json()
        s = client.post(
            f"/api/v1/projects/{p['id']}/scenes",
            json={"title": "S1", "style_kit_id": kit["id"]},
        ).json()
        sh = client.post(
            f"/api/v1/scenes/{s['id']}/shots",
            json={"action": "A lighthouse at dawn", "shot_type": "wide",
                  "movement": "dolly_in"},
        ).json()

        with session_scope() as db:
            asm = prompt_assembly.assemble_shot_prompt(db, uuid.UUID(sh["id"]))

        assert str(asm["style_kit_id"]) == kit["id"]
        style_medium = asm["sections"]["style_medium"]
        assert "35mm film grain, photographic" in style_medium
        assert "rule of thirds" in style_medium
        # Kit lens lands in the camera slot; color in color_mood.
        assert "35mm lens" in asm["sections"]["camera_lens"]
        assert "teal and orange" in asm["sections"]["color_mood"]
        assert asm["prompt_final"]  # joined prompt is non-empty
    finally:
        celery_app.conf.task_always_eager = False
        get_settings.cache_clear()
        reset_bridge()
        reset_engine()


def test_token_budget_read_from_constraints(tmp_path, monkeypatch):
    """max_prompt_tokens lives under constraints (seed), not capabilities.

    `_max_prompt_tokens` once read capabilities, so the SD1.5 77-token
    budget warning could never fire.
    """
    _api_env(tmp_path, monkeypatch)
    try:
        from app.services.prompt_assembly import _max_prompt_tokens

        assert _max_prompt_tokens("comfyui.t2i-sd15") == 77
        assert _max_prompt_tokens("nonexistent-model") is None
        assert _max_prompt_tokens(None) is None
    finally:
        celery_app.conf.task_always_eager = False
        get_settings.cache_clear()
        reset_bridge()
        reset_engine()
