"""B7 provider quality inputs: code contracts (ADR-0014, audit F08/F14/F15/F16).

Every provider is faked (httpx.MockTransport, fake adapters, a fake CLI
script) — no network, no real provider, no real sleeping (fake clock).

Probes covered (audit r1-codex P11-P19):
  P11 reference image reaches the provider submit body (and follows the
      canonical reference), P19 ComfyUI i2i/inpaint upload fills
      {uploaded_image}/{uploaded_mask}, capability pre-check (4xx before any
      row/reservation/dispatch), P12/P13 locked style authority, variations
      keep the seed, random seeds persisted, CLI negative prompt, P14 CLI
      attribution under concurrency, P15 total deadline + cancel, missing
      status, P16 result validation (Content-Type, byte caps, magic bytes),
      video probe failure fails the job.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy.orm import sessionmaker

import app.core.celery_app as celery_mod
import app.db.session as sess_mod
from app.core import defaults
from app.core.config import get_settings
from app.core.errors import ERROR_HTTP_STATUS, ErrorCode, StudioError
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.character import Character, CharacterReference
from app.models.generation import Generation, Video
from app.models.production import Scene, Shot, ShotReference
from app.models.project import Project
from app.models.style import StyleKit
from app.models.system import Model
from app.models_lib import registry
from app.models_lib.adapters import http_polling
from app.models_lib.adapters.cli import CliAdapter
from app.models_lib.adapters.http_polling import HttpPollingAdapter, poll_until_done
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter
from app.tasks import tasks_image, tasks_video  # noqa: F401 (registers dispatchers)
from tests.conftest import isolated_engine

TASK_NAMES = ("q.image.run_generation", "q.video.run_generation")
SEEDS = Path(__file__).resolve().parents[3] / "infra" / "seeds" / "models"


def _png(color=(10, 20, 30), size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url, engine = isolated_engine(tmp_path, "contract.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    from app.models_lib.seed import apply_seed

    apply_seed()  # real seed rows (stub + disabled comfyui/codex/...)
    sent: list[tuple[str, tuple]] = []
    for name in TASK_NAMES:
        monkeypatch.setitem(
            celery_mod._JOB_DISPATCHERS, name, lambda *a, _n=name: sent.append((_n, a))
        )
    monkeypatch.setattr(defaults, "RETRY_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(defaults, "RETRY_JITTER_SEC", 0.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_START_SEC", 0.0)
    monkeypatch.setenv("COMFYUI_URL", "http://comfy.test:8188/prompt")

    with session_scope() as db:
        project = Project(title="B7 contract")
        db.add(project)
        db.flush()
        pid = project.id
    yield {"sent": sent, "project_id": pid, "tmp": tmp_path}
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()
    engine.dispose()


def _activate(*model_ids: str) -> None:
    with session_scope() as db:
        for mid in model_ids:
            db.get(Model, mid).status = "active"


def _register_model(monkeypatch, model: dict) -> None:
    """A test-only model: DB row (FK) + seed cache (adapter lookups)."""
    cache = dict(registry._load_seed_cache())
    cache[model["model_id"]] = model
    monkeypatch.setattr(registry, "_seed_cache", cache)
    with session_scope() as db:
        db.add(
            Model(
                model_id=model["model_id"],
                modality=model["modality"],
                provider_id=model["provider_id"],
                status="active",
                capabilities=model["capabilities"],
                constraints={},
            )
        )


def _install_http(monkeypatch, handler) -> HttpPollingAdapter:
    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(registry._ADAPTERS, "http_polling", adapter)
    return adapter


def _write(env, rel: str, data: bytes) -> str:
    path = env["tmp"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def _get(model, row_id):
    with session_scope() as db:
        row = db.get(model, uuid.UUID(str(row_id)))
        db.expunge_all()
        return row


def _children(parent_id) -> list[Generation]:
    with session_scope() as db:
        rows = (
            db.query(Generation)
            .filter(Generation.parent_generation_id == uuid.UUID(str(parent_id)))
            .order_by(Generation.created_at, Generation.batch_index)
            .all()
        )
        db.expunge_all()
        return rows


def _run_sent_images(env) -> list[str]:
    ids = [a[0] for n, a in env["sent"] if n == "q.image.run_generation"]
    env["sent"].clear()
    for gid in ids:
        tasks_image.run_generation.apply(args=[gid])
    return ids


def _shot(env, *, action="hero walks", style_kit_id=None) -> uuid.UUID:
    with session_scope() as db:
        scene = Scene(project_id=env["project_id"], order_index=0)
        db.add(scene)
        db.flush()
        shot = Shot(
            scene_id=scene.id, order_index=0, action=action, style_kit_id=style_kit_id
        )
        db.add(shot)
        db.flush()
        return shot.id


def _character_on_shot(env, shot_id, image: bytes) -> tuple[uuid.UUID, str]:
    with session_scope() as db:
        char = Character(
            project_id=env["project_id"],
            name="Mira",
            appearance_prompt="Mira, red scarf",
            default_strength=0.8,
        )
        db.add(char)
        db.flush()
        rel = _write(
            env, f"projects/{env['project_id']}/characters/{char.id}/master.png", image
        )
        db.add(
            CharacterReference(
                character_id=char.id,
                image_path=rel,
                role="master",
                is_primary=True,
                source="uploaded",
            )
        )
        db.add(ShotReference(shot_id=shot_id, character_id=char.id, purpose="primary"))
        return char.id, rel


def _new_primary(env, char_id, image: bytes) -> str:
    with session_scope() as db:
        for old in (
            db.query(CharacterReference)
            .filter(CharacterReference.character_id == char_id)
            .all()
        ):
            old.is_primary = False
        db.flush()
        rel = _write(
            env,
            f"projects/{env['project_id']}/characters/{char_id}/master_v2.png",
            image,
        )
        db.add(
            CharacterReference(
                character_id=char_id,
                image_path=rel,
                role="master",
                is_primary=True,
                source="uploaded",
            )
        )
        return rel


def _approved_take(env, shot_id, image: bytes) -> uuid.UUID:
    with session_scope() as db:
        rel = _write(env, f"projects/{env['project_id']}/frames/take.png", image)
        gen = Generation(
            project_id=env["project_id"],
            shot_id=shot_id,
            job_type="text_to_image",
            model_id="stub.t2i-fast",
            status="succeeded",
            output_path=rel,
            prompt_final="a hero",
        )
        db.add(gen)
        db.flush()
        db.get(Shot, shot_id).approved_frame_generation_id = gen.id
        return gen.id


# A provider whose API takes one reference image inline (data URI).
REF_MODEL = {
    "model_id": "fake.ref-t2i",
    "provider_id": "http_polling",
    "modality": "image",
    "status": "active",
    "pricing": {"unit": "per_image", "amount_usd": "0.00"},
    "capabilities": {
        "tasks": ["text_to_image"],
        "inputs": ["negative_prompt", "seed", "reference_images"],
        "http": {
            "submit": {
                "url": "https://ref.test/v1/generate",
                "headers": {"Authorization": "env:FAKE_REF_KEY"},
                "body_template": {
                    "prompt": "{prompt}",
                    "negative_prompt": "{negative_prompt}",
                    "seed": "{seed}",
                    "width": "{width}",
                    "reference": "{reference_image_data_uri}",
                    "reference_weight": "{reference_weight}",
                },
            },
            "job_id_path": "id",
            "status": {
                "url_template": "https://ref.test/v1/jobs/{job_id}",
                "state_path": "state",
                "state_map": {"QUEUED": "queued", "DONE": "succeeded"},
            },
            "result": {"output_url_path": "url"},
            "cancel": {"url_template": "https://ref.test/v1/jobs/{job_id}/cancel"},
        },
    },
}


# Same provider without reference conditioning (bounds / validation tests).
PLAIN_MODEL = json.loads(json.dumps(REF_MODEL))
PLAIN_MODEL["model_id"] = "fake.plain-t2i"
PLAIN_MODEL["capabilities"]["inputs"] = ["negative_prompt", "seed"]
for _key in ("reference", "reference_weight"):
    PLAIN_MODEL["capabilities"]["http"]["submit"]["body_template"].pop(_key)


class _RefProvider:
    def __init__(self, *, state="DONE", output=None, tick=None):
        self.submits: list[dict] = []
        self.cancels = 0
        self.polls = 0
        self.state = state
        self.output = output if output is not None else _png()
        self.tick = tick  # callable advancing the fake clock per request

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.tick is not None:
            self.tick()
        if req.url.path == "/v1/generate":
            self.submits.append(json.loads(req.content))
            return httpx.Response(200, json={"id": f"job-{len(self.submits)}"})
        if req.url.path.endswith("/cancel"):
            self.cancels += 1
            return httpx.Response(200, json={})
        if req.url.path.startswith("/v1/jobs/"):
            self.polls += 1
            body = {"state": self.state}
            if self.state == "DONE":
                body["url"] = "https://cdn.ref.test/out.png"
            return httpx.Response(200, json=body)
        if req.url.host == "cdn.ref.test":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=self.output
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")


# ---------------------------------------------------------------------------
# F08 / P11: the reference image reaches the provider
# ---------------------------------------------------------------------------


def test_p11_shot_character_reference_reaches_submit_body(env, monkeypatch):
    from app.services import generations

    monkeypatch.setenv("FAKE_REF_KEY", "Bearer ref-secret")
    _register_model(monkeypatch, REF_MODEL)
    provider = _RefProvider()
    _install_http(monkeypatch, provider)
    shot_id = _shot(env)
    first = _png((200, 0, 0))
    char_id, rel = _character_on_shot(env, shot_id, first)

    with session_scope() as db:
        generations.enqueue_frame(db, shot_id, model_id="fake.ref-t2i")
    (gid,) = _run_sent_images(env)

    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    body = provider.submits[0]
    assert body["reference"] == _data_uri(first)
    assert body["reference_weight"] == 0.8  # character.default_strength
    assert body["seed"] == gen.seed
    snap = gen.params["snapshot"]
    assert snap["references"][0]["path"] == rel
    assert snap["references"][0]["role"] == "primary"
    assert snap["references_applied"] is True
    assert snap["model_id"] == "fake.ref-t2i" and snap["model_config_sha256"]

    # The canonical reference changes -> the next submit carries the new one.
    second = _png((0, 0, 200))
    _new_primary(env, char_id, second)
    with session_scope() as db:
        generations.enqueue_frame(db, shot_id, model_id="fake.ref-t2i")
    _run_sent_images(env)
    assert provider.submits[1]["reference"] == _data_uri(second)
    assert provider.submits[1]["reference"] != provider.submits[0]["reference"]


def test_reference_on_incapable_model_degrades_with_recorded_warning(env, monkeypatch):
    """comfyui.t2i-sd15 cannot take reference images: the job runs text-only
    and the snapshot says so explicitly (decision: degrade, not reject)."""
    from app.services import generations

    _activate("comfyui.t2i-sd15")
    shot_id = _shot(env)
    _character_on_shot(env, shot_id, _png())
    with session_scope() as db:
        generations.enqueue_frame(db, shot_id, model_id="comfyui.t2i-sd15")
    (gid,) = [a[0] for n, a in env["sent"]]
    gen = _get(Generation, gid)
    snap = gen.params["snapshot"]
    assert snap["references"] and snap["references_applied"] is False
    assert [w["code"] for w in snap["warnings"]] == ["reference_unsupported"]
    with session_scope() as db:
        payload = tasks_image._adapter_payload(db, db.get(Generation, gen.id))
    assert payload["references"] == [] and "reference_image" not in payload


def test_character_sheet_passes_approved_master_as_identity_reference(env):
    from app.services import character_pipeline

    with session_scope() as db:
        char = Character(
            project_id=env["project_id"], name="Mira", appearance_prompt="Mira"
        )
        db.add(char)
        db.flush()
        rel = _write(
            env, f"projects/{env['project_id']}/characters/{char.id}/master.png", _png()
        )
        db.add(
            CharacterReference(
                character_id=char.id,
                image_path=rel,
                role="master",
                is_primary=True,
                source="generated",
            )
        )
        db.flush()
        job_ids, _ = character_pipeline.enqueue_sheet(
            db, char, env["project_id"], ["turnaround_front"], None, None, None
        )
    gen = _get(Generation, job_ids[0])
    snap = gen.params["snapshot"]
    assert snap["references"][0]["path"] == rel
    assert snap["references"][0]["role"] == "identity"
    assert gen.seed is not None and snap["seed"] == gen.seed
    with session_scope() as db:
        payload = tasks_image._adapter_payload(db, db.get(Generation, gen.id))
    assert payload["reference_image"] == str((env["tmp"] / rel).resolve())


# ---------------------------------------------------------------------------
# P19: ComfyUI i2i / inpaint upload + placeholders
# ---------------------------------------------------------------------------


class _Comfy:
    def __init__(self, out_node: str):
        self.uploads: list[bytes] = []
        self.workflow: dict | None = None
        self.out_node = out_node

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/upload/image":
            self.uploads.append(req.content)
            return httpx.Response(200, json={"name": f"up_{len(self.uploads)}.png"})
        if req.url.path == "/prompt":
            self.workflow = json.loads(req.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1"})
        if req.url.path == "/history/p1":
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "status": {
                            "status_str": "success",
                            "completed": True,
                            "messages": [],
                        },
                        "outputs": {
                            self.out_node: {
                                "images": [
                                    {
                                        "filename": "o.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if req.url.path == "/view":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=_png()
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")


def test_p19_comfyui_i2i_uploads_init_image_and_fills_placeholder(env, monkeypatch):
    """The audit's P19 row shape (stored input_paths, no snapshot): the
    worker must send the contract key init_image so the upload happens."""
    _activate("comfyui.i2i-sd15")
    comfy = _Comfy("8")
    _install_http(monkeypatch, comfy)
    src = _write(env, f"projects/{env['project_id']}/frames/src.png", _png((1, 2, 3)))
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="image_to_image",
            model_id="comfyui.i2i-sd15",
            prompt_final="detail",
            input_paths=[src],
            params={},
            seed=42,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    assert len(comfy.uploads) == 1 and b"src.png" in comfy.uploads[0]
    assert comfy.workflow["4"]["inputs"]["image"] == "up_1.png"
    assert comfy.workflow["6"]["inputs"]["seed"] == 42


def test_p19_comfyui_inpaint_uploads_image_and_mask(env, monkeypatch):
    from app.services import generations

    _activate("comfyui.inpaint-sd15")
    comfy = _Comfy("9")
    _install_http(monkeypatch, comfy)
    shot_id = _shot(env)
    take = _approved_take(env, shot_id, _png())
    mask = _write(
        env, f"projects/{env['project_id']}/masks/mask.png", _png((255, 255, 255))
    )
    with session_scope() as db:
        generations.enqueue_inpaint(
            db,
            shot_id,
            generation_id=take,
            mask_path=mask,
            prompt="fix the hand",
            model_id="comfyui.inpaint-sd15",
        )
    (gid,) = _run_sent_images(env)
    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    assert len(comfy.uploads) == 2
    assert b"take.png" in comfy.uploads[0] and b"mask.png" in comfy.uploads[1]
    assert comfy.workflow["4"]["inputs"]["image"] == "up_1.png"  # {uploaded_image}
    assert comfy.workflow["5"]["inputs"]["image"] == "up_2.png"  # {uploaded_mask}
    assert gen.params["snapshot"]["mask"] == mask
    assert gen.params["snapshot"]["init_image"].endswith("take.png")


# ---------------------------------------------------------------------------
# Capability pre-check: 4xx before any row / reservation / dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call,model_id",
    [
        ("inpaint", "comfyui.i2i-sd15"),  # i2i model has no mask input
        ("upscale", "comfyui.t2i-sd15"),  # t2i-only model
        ("outpaint", "codex.t2i-gptimage2"),
    ],
)
def test_unsupported_editing_task_rejected_before_any_row(
    env, monkeypatch, call, model_id
):
    from app.services import budget_guard, generations

    _activate(model_id)
    shot_id = _shot(env)
    take = _approved_take(env, shot_id, _png())
    mask = _write(env, f"projects/{env['project_id']}/masks/m.png", _png())
    budget_calls: list = []
    monkeypatch.setattr(
        budget_guard, "check_budget", lambda *a, **k: budget_calls.append((a, k))
    )
    with session_scope() as db:
        before = db.query(Generation).count()
    kwargs = {"generation_id": take, "model_id": model_id}
    if call in ("inpaint", "outpaint"):
        kwargs.update(prompt="fix", mask_path=mask)
    with pytest.raises(StudioError) as ei:
        with session_scope() as db:
            getattr(generations, f"enqueue_{call}")(db, shot_id, **kwargs)
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert 400 <= ERROR_HTTP_STATUS[ei.value.code] < 500
    assert ei.value.details["reason"] in ("task_unsupported", "input_unsupported")
    with session_scope() as db:
        assert db.query(Generation).count() == before  # no job row
        _, reserved = budget_guard.committed_usd(db, env["project_id"])
    assert reserved == Decimal("0")  # no reservation
    assert budget_calls == []  # rejected before the budget step
    assert env["sent"] == []  # no dispatch


def test_editing_input_never_silently_ignored_by_adapters(tmp_path, monkeypatch):
    """Defense in depth: an adapter given an editing input it cannot take
    rejects it before any HTTP call / process spawn."""
    model = json.loads((SEEDS / "comfyui.json").read_text())["models"][0]  # t2i
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    monkeypatch.setenv("COMFYUI_URL", "http://comfy.test:8188/prompt")
    calls: list = []
    adapter = HttpPollingAdapter(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))
    )
    src = tmp_path / "s.png"
    src.write_bytes(_png())
    with pytest.raises(StudioError) as ei:
        adapter.submit(
            "comfyui.t2i-sd15",
            {"prompt": "x", "seed": 1, "width": 8, "height": 8, "init_image": str(src)},
        )
    assert ei.value.code == ErrorCode.VALIDATION_ERROR and calls == []
    from unittest import mock

    cli_model = _cli_model("codex", str(tmp_path))
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: cli_model)
    spawned: list = []
    with (
        mock.patch(
            "app.models_lib.adapters.cli.shutil.which", return_value="/bin/codex"
        ),
        mock.patch.object(CliAdapter, "_check_login", lambda *a: None),
        mock.patch(
            "app.models_lib.adapters.cli.subprocess.Popen",
            lambda *a, **k: spawned.append(a),
        ),
    ):
        with pytest.raises(StudioError) as ei:
            CliAdapter().submit(
                "codex.t2i-gptimage2", {"prompt": "x", "mask": str(src)}
            )
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.details["inputs"] == ["mask"] and spawned == []


# ---------------------------------------------------------------------------
# F15: style authority, seeds, snapshot
# ---------------------------------------------------------------------------


def _kit(
    env, *, locked: bool, texture: str, composition: str | None = None
) -> uuid.UUID:
    with session_scope() as db:
        kit = StyleKit(
            project_id=env["project_id"],
            name=texture,
            version=1,
            is_locked=locked,
            texture=texture,
            composition=composition,
        )
        db.add(kit)
        db.flush()
        return kit.id


def test_p12_locked_project_kit_is_the_style_authority(env):
    from app.services import prompt_assembly

    locked = _kit(env, locked=True, texture="LOCKED_FILM")
    other = _kit(env, locked=False, texture="SHOT_CARTOON")
    with session_scope() as db:
        db.get(Project, env["project_id"]).style_kit_id = locked
    plain = _shot(env)
    with session_scope() as db:
        out = prompt_assembly.assemble_shot_prompt(db, plain)
    assert out["style_kit_id"] == locked
    assert "LOCKED_FILM" in out["prompt_final"]

    # A shot-level kit cannot replace a locked project kit (ADR-0011 §13).
    shot_kit = _shot(env, style_kit_id=other)
    with session_scope() as db:
        out = prompt_assembly.assemble_shot_prompt(db, shot_kit)
    assert out["style_kit_id"] == locked
    assert "SHOT_CARTOON" not in out["prompt_final"]


def test_p13_locked_slots_not_overridable_or_truncated(env, monkeypatch):
    from app.services import prompt_assembly

    locked = _kit(env, locked=True, texture="LOCKED_FILM", composition="rule of thirds")
    shot_id = _shot(env, style_kit_id=locked)
    with session_scope() as db:
        with pytest.raises(StudioError) as ei:
            prompt_assembly.assemble_shot_prompt(
                db, shot_id, overrides={"style_medium": "OVERRIDE_CARTOON"}
            )
        assert ei.value.code == ErrorCode.STYLE_KIT_LOCKED
        with pytest.raises(StudioError):
            prompt_assembly.assemble_shot_prompt(
                db, shot_id, overrides={"style_medium": None}
            )
        # Non-kit slots stay overridable.
        out = prompt_assembly.assemble_shot_prompt(
            db, shot_id, overrides={"setting": "harbor"}
        )
        assert "harbor" in out["prompt_final"] and "LOCKED_FILM" in out["prompt_final"]

        # Truncation drops setting but never the locked kit's tokens.
        monkeypatch.setattr(prompt_assembly, "_max_prompt_chars", lambda m: 120)
        out = prompt_assembly.assemble_shot_prompt(
            db, shot_id, overrides={"setting": "x" * 200}
        )
        assert "setting" in out["truncated_sections"]
        assert "style_medium:details" not in out["truncated_sections"]
        assert "rule of thirds" in out["prompt_final"]

    # Unlocked kits keep the old behavior: override + detail truncation allowed.
    free = _kit(env, locked=False, texture="FREE_FILM", composition="centered")
    free_shot = _shot(env, style_kit_id=free)
    with session_scope() as db:
        out = prompt_assembly.assemble_shot_prompt(
            db, free_shot, overrides={"style_medium": "OVERRIDE_CARTOON"}
        )
    assert (
        "OVERRIDE_CARTOON" in out["prompt_final"]
        and "FREE_FILM" not in out["prompt_final"]
    )


def test_prompt_variations_keep_the_seed_fixed(env):
    from app.services import generations

    shot_id = _shot(env)
    with session_scope() as db:
        out = generations.enqueue_variations(
            db, shot_id, count=3, variation_axis="prompt", seed=77
        )
    assert [v["seed"] for v in out["variations"]] == [77, 77, 77]
    assert len({v["prompt_final"] for v in out["variations"]}) == 3
    for parent in out["variation_job_ids"]:
        (child,) = _children(parent)
        assert child.seed == 77 and child.params["snapshot"]["seed"] == 77
    # Seed axis is unchanged: consecutive seeds, one prompt.
    with session_scope() as db:
        out = generations.enqueue_variations(
            db, shot_id, count=2, variation_axis="seed", seed=5
        )
    assert [v["seed"] for v in out["variations"]] == [5, 6]
    assert [c.params["snapshot"]["seed"] for c in _children(out["job_id"])] == [5, 6]


class _CapturingAdapter(ProviderAdapter):
    provider_id = "capture"

    def __init__(self):
        self.payloads: list[dict] = []

    def submit(self, model_id, payload):
        self.payloads.append(dict(payload))
        return f"cap-{len(self.payloads)}"

    def poll(self, provider_job_id):
        return JobStatus(state="succeeded", progress_pct=100.0)

    def cancel(self, provider_job_id):
        return None

    def estimate(self, model_id, payload):
        return EstimatedCost(Decimal("0"))

    def fetch_result(self, provider_job_id):
        return _png()


def test_random_seed_persisted_at_enqueue_and_used_by_worker(env, monkeypatch):
    from app.services import character_pipeline, generations

    capture = _CapturingAdapter()
    monkeypatch.setitem(registry._ADAPTERS, "stub", capture)
    shot_id = _shot(env)
    take = _approved_take(env, shot_id, _png())
    with session_scope() as db:
        out = generations.enqueue_upscale(db, shot_id, generation_id=take)
    up = _get(Generation, out["job_id"])
    assert isinstance(up.seed, int)
    snap = up.params["snapshot"]
    assert snap["seed"] == up.seed and snap["seed_source"] == "random"
    assert snap["init_image"].endswith("take.png")
    assert snap["width"] == defaults.IMAGE_DEFAULT_WIDTH and "sampler" in snap

    with session_scope() as db:
        char = Character(project_id=env["project_id"], name="M", appearance_prompt="M")
        db.add(char)
        db.flush()
        master_id = character_pipeline.enqueue_master(
            db, char, None, None, env["project_id"]
        )
    master = _get(Generation, master_id)
    assert (
        isinstance(master.seed, int)
        and master.params["snapshot"]["seed"] == master.seed
    )

    _run_sent_images(env)
    seeds = {p["seed"] for p in capture.payloads}
    assert seeds == {up.seed, master.seed}
    upscale_payload = next(p for p in capture.payloads if p["task"] == "upscale")
    assert upscale_payload["init_image"].endswith("take.png")
    assert "images" not in upscale_payload  # one contract key (P19)


def test_legacy_row_without_seed_gets_one_persisted_before_submit(env, monkeypatch):
    capture = _CapturingAdapter()
    monkeypatch.setitem(registry._ADAPTERS, "stub", capture)
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="stub.t2i-fast",
            prompt_final="x",
            params={},
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "succeeded" and row.seed == capture.payloads[0]["seed"]


# ---------------------------------------------------------------------------
# CLI adapter: negative prompt, attribution (F14 / P14)
# ---------------------------------------------------------------------------


def _cli_model(command: str, workdir: str) -> dict:
    model = json.loads((SEEDS / "cli.json").read_text())["models"][0]
    model = json.loads(json.dumps(model))
    model["capabilities"]["cli"].update(command=command, workdir=workdir)
    return model


def test_cli_negative_prompt_reaches_the_instruction(tmp_path, monkeypatch):
    from unittest import mock

    seen: dict = {}

    class _Proc:
        pid = 1

        def __init__(self, argv, **kw):
            seen["argv"] = argv

        def poll(self):
            return None

    model = _cli_model("codex", str(tmp_path))
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    with (
        mock.patch(
            "app.models_lib.adapters.cli.shutil.which", return_value="/bin/codex"
        ),
        mock.patch.object(CliAdapter, "_check_login", lambda *a: None),
        mock.patch("app.models_lib.adapters.cli.subprocess.Popen", _Proc),
    ):
        CliAdapter().submit(
            "codex.t2i-gptimage2",
            {"prompt": "a lighthouse", "negative_prompt": "blurry hands, watermark"},
        )
    instruction = seen["argv"][-1]
    assert "a lighthouse" in instruction
    assert "blurry hands, watermark" in instruction


_FAKE_CLI = r"""
import os, re, sys, time, uuid
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in"); sys.exit(0)
instruction = args[-1]
marker = re.search(r"MARKER=(\w+)", instruction).group(1)
delay = os.environ.get("FAKE_CLI_DELAY_" + marker, os.environ.get("FAKE_CLI_DELAY", "0.3"))
time.sleep(float(delay))
png = b"\x89PNG\r\n\x1a\n" + marker.encode()
shared = os.path.join(os.environ["CODEX_HOME"], "generated_images", uuid.uuid4().hex)
os.makedirs(shared, exist_ok=True)
with open(os.path.join(shared, "ig.png"), "wb") as f:
    f.write(png)
shared_only = os.environ.get("FAKE_CLI_MODE") == "shared-only" or marker in (
    os.environ.get("FAKE_CLI_SHARED_ONLY", "").split(",")
)
m = re.search(r"exactly this path: (\S+)", instruction)
if m and not shared_only:
    with open(m.group(1), "wb") as f:
        f.write(png)
"""


def _fake_cli(tmp_path) -> str:
    script = tmp_path / "fake_codex"
    script.write_text(f"#!{sys.executable}\n{_FAKE_CLI}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


_CLI_MARKERS = ("alpha", "beta")


def _cli_jobs_outcome(
    tmp_path, monkeypatch, mode: str, *, poll_order=(0, 1), shared_only: str = ""
) -> tuple[dict, dict, dict]:
    """Two real fake-CLI processes (alpha finishes first, beta second); both
    have exited — every output exists — before any poll. Polls in
    ``poll_order``. Returns ({marker: JobStatus}, {marker: bytes | None},
    {marker: result path | None})."""
    cmd = _fake_cli(tmp_path)
    (tmp_path / "work").mkdir()
    model = _cli_model(cmd, str(tmp_path / "work"))
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    monkeypatch.setenv("FAKE_CLI_SHARED_ONLY", shared_only)
    monkeypatch.setenv("FAKE_CLI_DELAY_alpha", "0.1")
    monkeypatch.setenv("FAKE_CLI_DELAY_beta", "0.5")
    adapter = CliAdapter()
    jobs = {
        m: adapter.submit("codex.t2i-gptimage2", {"prompt": f"scene MARKER={m}"})
        for m in _CLI_MARKERS
    }
    deadline = time.monotonic() + 20
    while any(adapter._jobs[j].proc.poll() is None for j in jobs.values()):
        assert time.monotonic() < deadline, "fake CLI did not finish"
        time.sleep(0.05)
    statuses = {}
    for i in poll_order:
        marker = _CLI_MARKERS[i]
        statuses[marker] = adapter.poll(jobs[marker])
    paths = {m: adapter._jobs[jobs[m]].png_path for m in _CLI_MARKERS}
    results = {
        m: adapter.fetch_result(jobs[m]) if statuses[m].state == "succeeded" else None
        for m in _CLI_MARKERS
    }
    return statuses, results, paths


def _run_two_cli_jobs(tmp_path, monkeypatch, mode: str) -> list[bytes]:
    statuses, results, paths = _cli_jobs_outcome(tmp_path, monkeypatch, mode)
    assert [statuses[m].state for m in _CLI_MARKERS] == ["succeeded", "succeeded"]
    assert paths["alpha"] != paths["beta"]
    return [results[m] for m in _CLI_MARKERS]


def test_p14_concurrent_cli_jobs_each_get_their_own_file(tmp_path, monkeypatch):
    results = _run_two_cli_jobs(tmp_path, monkeypatch, "explicit")
    assert results[0].endswith(b"alpha") and results[1].endswith(b"beta")


def test_p14_shared_dir_fallback_never_hands_one_file_to_two_jobs(
    tmp_path, monkeypatch
):
    # B7-r2 H1: the CLI ignored the explicit per-job path. Nothing in the
    # shared folder proves which job produced which file, so no job takes
    # one (formerly: each claimed "some" file — the jobs could swap).
    statuses, results, _ = _cli_jobs_outcome(tmp_path, monkeypatch, "shared-only")
    for marker in _CLI_MARKERS:
        assert statuses[marker].state == "failed"
        assert statuses[marker].error_code == ErrorCode.PROVIDER_ERROR
        assert "per-job output file" in statuses[marker].error_message
        assert results[marker] is None


@pytest.mark.parametrize("poll_order", [(0, 1), (1, 0)], ids=["alpha-first", "beta-first"])
@pytest.mark.parametrize(
    "mode,shared_only,expected",
    [
        ("explicit", "", {"alpha": "succeeded", "beta": "succeeded"}),
        ("shared-only", "", {"alpha": "failed", "beta": "failed"}),
        ("explicit", "beta", {"alpha": "succeeded", "beta": "failed"}),
        ("explicit", "alpha", {"alpha": "failed", "beta": "succeeded"}),
    ],
    ids=["both-explicit", "both-ignore-path", "beta-ignores-path", "alpha-ignores-path"],
)
def test_b7r2_h1_cli_result_carries_its_own_jobs_marker(
    tmp_path, monkeypatch, poll_order, mode, shared_only, expected
):
    """B7-r2 H1 (review P01): per job — never a sorted set — a job's result
    is the bytes ITS process wrote, whatever the poll order; a job whose
    CLI ignored the per-job path fails instead of taking a shared file."""
    statuses, results, _ = _cli_jobs_outcome(
        tmp_path, monkeypatch, mode, poll_order=poll_order, shared_only=shared_only
    )
    for marker in _CLI_MARKERS:
        assert statuses[marker].state == expected[marker], (marker, statuses[marker])
        if expected[marker] == "succeeded":
            assert results[marker] == b"\x89PNG\r\n\x1a\n" + marker.encode(), (
                f"job {marker} received {results[marker]!r}"
            )
        else:
            assert results[marker] is None
            assert "cannot be attributed" in statuses[marker].error_message


# ---------------------------------------------------------------------------
# F16 / P15: total deadline, cancel, missing status (fake clock)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, sec):
        self.slept += sec
        self.now += sec


def test_p15_total_deadline_cancels_and_fails_uncertain(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic, raising=False)
    monkeypatch.setattr(http_polling, "_sleep", clock.sleep, raising=False)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 30.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_MAX_SEC", 0.0)
    monkeypatch.setattr(defaults, "POLL_MAX_ITERATIONS", 50)
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    _register_model(monkeypatch, PLAIN_MODEL)

    def tick():
        clock.now += 5.0  # each provider request "takes" 5s

    provider = _RefProvider(state="QUEUED", tick=tick)
    _install_http(monkeypatch, provider)
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.plain-t2i",
            prompt_final="x",
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    started = time.monotonic()
    result = tasks_image.run_generation.apply(args=[gid])
    assert result.failed()
    assert time.monotonic() - started < 5  # no real sleeping
    row = _get(Generation, gid)
    assert row.status == "failed"
    assert row.error_code == ErrorCode.PROVIDER_UNCERTAIN  # D7: outcome unknown
    assert "deadline" in row.error_message
    assert provider.cancels == 1  # best-effort provider cancel
    assert len(provider.submits) == 1 and row.retry_count == 0  # never resubmitted
    assert provider.polls < 50  # stopped by the deadline, not the iteration cap


def test_poll_sleep_never_overshoots_the_deadline(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic)
    monkeypatch.setattr(http_polling, "_sleep", clock.sleep)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 25.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_START_SEC", 2.0)

    class _Queued:
        cancels = 0

        def poll(self, pid):
            return JobStatus(state="queued")

        def cancel(self, pid):
            self.cancels += 1

    adapter = _Queued()
    with pytest.raises(StudioError) as ei:
        poll_until_done(adapter, "x")
    assert ei.value.details["reason"] == "deadline_exceeded"
    assert clock.slept == pytest.approx(25.0) and adapter.cancels == 1


def test_missing_status_is_an_error_not_queued_forever(env, monkeypatch):
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    _register_model(monkeypatch, PLAIN_MODEL)
    cancels: list = []

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "j1"})
        if req.url.path.endswith("/cancel"):
            cancels.append(1)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"something": "else"})  # no state

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit(
        "fake.plain-t2i",
        {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
    )
    with pytest.raises(StudioError) as ei:
        poll_until_done(adapter, pid)
    assert ei.value.details["reason"] == "status_missing"
    assert cancels == [1]


# ---------------------------------------------------------------------------
# F16 / P16: result validation
# ---------------------------------------------------------------------------


class _Body(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read += len(chunk)
            yield chunk


def _fetch(
    monkeypatch, download: httpx.Response, *, inline: str | None = None
) -> bytes:
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    model = json.loads(json.dumps(PLAIN_MODEL))
    if inline is not None:
        model["capabilities"]["http"]["result"] = {"output_bytes_path": "b64"}
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "j1"})
        if req.url.path == "/v1/jobs/j1":
            body = {"state": "DONE", "url": "https://cdn.ref.test/o"}
            if inline is not None:
                body["b64"] = inline
            return httpx.Response(200, json=body)
        return download

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit(
        "fake.plain-t2i",
        {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
    )
    return adapter.fetch_result(pid)


def test_p16_html_200_is_rejected(monkeypatch):
    html = httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html>no</html>"
    )
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, html)
    assert ei.value.code == ErrorCode.PROVIDER_ERROR
    # Same HTML with a lying image/png Content-Type: magic bytes catch it.
    lying = httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"<html>no</html>"
    )
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, lying)
    assert ei.value.details["reason"] == "result_not_media"
    with pytest.raises(StudioError):
        _fetch(monkeypatch, None, inline=base64.b64encode(b"<html>no</html>").decode())


def test_p16_content_length_over_cap_rejected_before_download(monkeypatch):
    monkeypatch.setattr(defaults, "HTTP_RESULT_MAX_BYTES_IMAGE", 1024)
    body = _Body([_png()])
    resp = httpx.Response(
        200,
        headers={"content-type": "image/png", "content-length": "4096"},
        stream=body,
    )
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, resp)
    assert ei.value.details["reason"] == "result_too_large"
    assert body.read == 0  # rejected from the header, body never read


def test_p16_streamed_bytes_over_cap_rejected_without_or_with_lying_length(monkeypatch):
    monkeypatch.setattr(defaults, "HTTP_RESULT_MAX_BYTES_IMAGE", 1024)
    head = _png()
    chunks = [head] + [b"\0" * 512] * 8  # > 1 KiB, no Content-Length
    no_length = _Body(chunks)
    with pytest.raises(StudioError) as ei:
        _fetch(
            monkeypatch,
            httpx.Response(
                200, headers={"content-type": "image/png"}, stream=no_length
            ),
        )
    assert ei.value.details["reason"] == "result_too_large"
    assert no_length.read < sum(len(c) for c in chunks)  # stopped mid-stream
    lying = _Body(list(chunks))
    with pytest.raises(StudioError) as ei:
        _fetch(
            monkeypatch,
            httpx.Response(
                200,
                headers={"content-type": "image/png", "content-length": "10"},
                stream=lying,
            ),
        )
    assert ei.value.details["reason"] == "result_too_large"


def test_p16_valid_png_passes(monkeypatch):
    png = _png()
    ok = httpx.Response(200, headers={"content-type": "image/png"}, content=png)
    assert _fetch(monkeypatch, ok) == png
    assert _fetch(monkeypatch, None, inline=_data_uri(png)) == png


def test_error_snippets_redact_secrets(monkeypatch):
    monkeypatch.setenv("FAKE_REF_KEY", "Bearer sk-live-SUPERSECRET123")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: PLAIN_MODEL)

    def handler(req):
        echoed = req.headers["authorization"]
        return httpx.Response(
            500, text=f'denied for {echoed}; {{"api_key": "other-secret-999"}}'
        )

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(StudioError) as ei:
        adapter.submit(
            "fake.plain-t2i",
            {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
        )
    message = ei.value.message
    assert "SUPERSECRET123" not in message and "other-secret-999" not in message
    assert "[REDACTED]" in message


# ---------------------------------------------------------------------------
# Video: a failed probe of the downloaded clip fails the job
# ---------------------------------------------------------------------------


class _VideoAdapter(_CapturingAdapter):
    def __init__(self, data: bytes):
        super().__init__()
        self.data = data

    def fetch_result(self, provider_job_id):
        return self.data


def _video_job(env) -> tuple[str, str]:
    shot_id = _shot(env)
    with session_scope() as db:
        db.get(Shot, shot_id).status = "video_generating"
        gen = Generation(
            project_id=env["project_id"],
            shot_id=shot_id,
            job_type="text_to_video",
            model_id="stub.t2v-fast",
            status="queued",
            prompt_final="pan",
            params={"mode": "t2v", "motion_prompt": "pan", "duration_sec": 5.0},
        )
        db.add(gen)
        db.flush()
        video = Video(
            shot_id=shot_id,
            generation_id=gen.id,
            mode="t2v",
            motion_prompt="pan",
            duration_sec=5.0,
            fps=24,
            resolution="1920x1080",
            model_id="stub.t2v-fast",
            output_path="",
            status="generating",
        )
        db.add(video)
        db.flush()
        return str(gen.id), str(video.id)


_MP4_HEAD = b"\0\0\0\x20ftypisom" + b"\0" * 64


@pytest.mark.parametrize(
    "probe_mode,data,expected",
    [
        ("broken", _MP4_HEAD, "failed"),  # ffprobe rejects the file
        ("no-duration", _MP4_HEAD, "failed"),  # parsed but not playable
        ("missing-binary", b"<html>x</html>", "failed"),  # magic bytes fallback
        ("missing-binary", _MP4_HEAD, "succeeded"),
        ("ok", _MP4_HEAD, "succeeded"),
    ],
)
def test_video_probe_failure_fails_the_job(
    env, monkeypatch, probe_mode, data, expected
):
    from app.media import ffmpeg_ops

    monkeypatch.setitem(registry._ADAPTERS, "stub", _VideoAdapter(data))
    monkeypatch.setattr(
        ffmpeg_ops,
        "extract_frame",
        lambda *a, **k: (_ for _ in ()).throw(
            StudioError(ErrorCode.VALIDATION_ERROR, "no frame")
        ),
    )

    def probe(path):
        if probe_mode == "broken":
            raise ffmpeg_ops.FFmpegError(
                ErrorCode.PROVIDER_ERROR, "ffprobe failed: moov atom"
            )
        if probe_mode == "missing-binary":
            err = ffmpeg_ops.FFmpegError(
                ErrorCode.PROVIDER_ERROR, "ffprobe binary not found"
            )
            raise err from FileNotFoundError("ffprobe")
        if probe_mode == "no-duration":
            return {"duration_sec": 0.0, "width": 0, "height": 0, "fps": 0}
        return {"duration_sec": 4.0, "width": 640, "height": 360, "fps": 24.0}

    monkeypatch.setattr(ffmpeg_ops, "probe", probe)
    gid, vid = _video_job(env)
    tasks_video.run_generation.apply(args=[gid])
    gen, video = _get(Generation, gid), _get(Video, vid)
    assert gen.status == expected, (gen.error_code, gen.error_message)
    if expected == "failed":
        assert video.status == "failed"
        assert gen.error_code == ErrorCode.PROVIDER_ERROR
    else:
        assert video.status == "done"
        if probe_mode == "ok":
            assert video.resolution == "640x360" and video.duration_sec == 4.0


def test_contract_helpers_are_pure_and_documented():
    """The ADR-0014 contract keys are the ones adapters read."""
    from app.models_lib import contract

    snap = {
        "task": "inpaint",
        "width": 64,
        "height": 32,
        "init_image": "a.png",
        "mask": "m.png",
        "references": [{"path": "r.png", "role": "primary", "weight": 0.5}],
        "references_applied": True,
    }
    payload = contract.image_payload(
        snap,
        prompt="p",
        negative_prompt=None,
        seed=3,
        resolve=lambda r: os.path.join("/x", r),
    )
    assert payload == {
        "task": "inpaint",
        "prompt": "p",
        "negative_prompt": "",
        "seed": 3,
        "width": 64,
        "height": 32,
        "init_image": "/x/a.png",
        "mask": "/x/m.png",
        "references": [{"path": "/x/r.png", "role": "primary", "weight": 0.5}],
        "reference_image": "/x/r.png",
        "reference_weight": 0.5,
    }


# ---------------------------------------------------------------------------
# B7-r2: review findings H2 (absolute deadline inside a request), H3 (ComfyUI
# upload names), M1 (redaction of 200/failed text), M2 (inline caps before
# decode), M3 (images must decode), M4 (executed config), L1 (tasks: [])
# ---------------------------------------------------------------------------

_PROXY_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


class _DripServer:
    """Real localhost HTTP server (127.0.0.1 only). Path prefix picks the
    behavior:

    ``/ok/...``     fast JSON {"id": "j1", "state": "QUEUED"}
    ``/drip/...``   200 + headers at once, then the JSON body 1 byte / 50 ms
    ``/hdrip/...``  the status line + headers themselves 1 byte / 50 ms
    ``/edrip/...``  500 + an error body 1 byte / 50 ms
    """

    BODY = b'{"id": "j1", "state": "QUEUED", "pad": "' + b"x" * 200 + b'"}'
    INTERVAL = 0.05

    def __init__(self):
        import http.server
        import threading

        hits: list[tuple[str, str]] = []
        self.hits = hits
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _drip(self, data: bytes) -> None:
                for i in range(len(data)):
                    self.wfile.write(data[i : i + 1])
                    self.wfile.flush()
                    time.sleep(outer.INTERVAL)

            def _serve(self):
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                hits.append((self.command, self.path))
                body = outer.BODY
                try:
                    if self.path.startswith("/ok/"):
                        self.send_response(200)
                        self.send_header("content-type", "application/json")
                        self.send_header("content-length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif self.path.startswith("/drip/"):
                        self.send_response(200)
                        self.send_header("content-type", "application/json")
                        self.send_header("content-length", str(len(body)))
                        self.end_headers()
                        self._drip(body)
                    elif self.path.startswith("/hdrip/"):
                        head = (
                            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                            b"x-pad: " + b"y" * 200 + b"\r\n"
                            b"content-length: " + str(len(body)).encode() + b"\r\n\r\n"
                        )
                        self._drip(head)
                        self.wfile.write(body)
                    else:  # /edrip/
                        err = b"upstream failure " * 20
                        self.send_response(500)
                        self.send_header("content-type", "text/plain")
                        self.send_header("content-length", str(len(err)))
                        self.end_headers()
                        self._drip(err)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (that is the point)

            do_GET = _serve
            do_POST = _serve
            do_PUT = _serve

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a client hanging up mid-drip is expected

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def drip_server(monkeypatch):
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    server = _DripServer()
    yield server
    server.close()


def _drip_model(base: str, *, submit: str, status: str, cancel: str) -> dict:
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["model_id"] = "fake.drip-t2i"
    http = model["capabilities"]["http"]
    http["submit"]["url"] = f"{base}/{submit}/v1/generate"
    http["submit"]["headers"] = {}
    http["status"]["url_template"] = f"{base}/{status}/v1/jobs/{{job_id}}"
    http["cancel"] = {"url_template": f"{base}/{cancel}/v1/jobs/{{job_id}}/cancel"}
    return model


_DRIP_DEADLINE = 0.6
_DRIP_MARGIN = 0.6  # scheduling slack; the full drip takes ~10 s


@pytest.mark.parametrize(
    "case,submit,status,cancel",
    [
        ("submit-json-body", "drip", "ok", "ok"),
        ("submit-headers", "hdrip", "ok", "ok"),
        ("submit-error-body", "edrip", "ok", "ok"),
        ("status-json-body", "ok", "drip", "ok"),
    ],
)
def test_b7r2_h2_slow_drip_cannot_stretch_a_call_past_the_deadline(
    drip_server, monkeypatch, case, submit, status, cancel
):
    """B7-r2 H2 (review P02): a real socket that sends one byte every 50 ms
    never trips a per-read timeout; the call must still end at the job's
    absolute deadline (body, headers and error bodies alike)."""
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", _DRIP_DEADLINE)
    model = _drip_model(drip_server.base, submit=submit, status=status, cancel=cancel)
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    adapter = HttpPollingAdapter()  # real transport
    payload = {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8}
    started = time.monotonic()
    with pytest.raises(StudioError) as ei:
        pid = adapter.submit("fake.drip-t2i", payload)
        adapter.poll(pid)
    elapsed = time.monotonic() - started
    assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
    assert elapsed < _DRIP_DEADLINE + _DRIP_MARGIN, f"{case}: call took {elapsed:.2f}s"


def test_b7r2_h2_cancel_has_its_own_short_absolute_cap(drip_server, monkeypatch):
    monkeypatch.setattr(defaults, "HTTP_CANCEL_TIMEOUT_SEC", 0.4)
    model = _drip_model(drip_server.base, submit="ok", status="ok", cancel="drip")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    adapter = HttpPollingAdapter()
    pid = adapter.submit(
        "fake.drip-t2i",
        {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
    )
    started = time.monotonic()
    adapter.cancel(pid)  # best effort: never raises
    elapsed = time.monotonic() - started
    assert ("POST", "/drip/v1/jobs/j1/cancel") in drip_server.hits
    assert elapsed < 0.4 + _DRIP_MARGIN, f"cancel took {elapsed:.2f}s"


def test_b7r2_h2_dripping_submit_fails_uncertain_and_is_never_resubmitted(
    env, drip_server, monkeypatch
):
    """D7 end to end: the submit request was sent, its response never
    finished before the deadline -> failed + PROVIDER_UNCERTAIN, one
    submit, no automatic retry."""
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", _DRIP_DEADLINE)
    model = _drip_model(drip_server.base, submit="drip", status="ok", cancel="ok")
    _register_model(monkeypatch, model)
    monkeypatch.setitem(registry._ADAPTERS, "http_polling", HttpPollingAdapter())
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.drip-t2i",
            prompt_final="x",
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    started = time.monotonic()
    tasks_image.run_generation.apply(args=[gid])
    elapsed = time.monotonic() - started
    row = _get(Generation, gid)
    assert row.status == "failed"
    assert row.error_code == ErrorCode.PROVIDER_UNCERTAIN, row.error_message
    assert "deadline" in row.error_message
    assert row.retry_count == 0
    submits = [h for h in drip_server.hits if h[1].endswith("/v1/generate")]
    assert len(submits) == 1  # never resubmitted
    assert elapsed < _DRIP_DEADLINE + 3.0  # worker bookkeeping on top


# --- H3: ComfyUI uploads ----------------------------------------------------


class _ComfyStore:
    """ComfyUI /upload/image storage semantics (server.py image_upload):
    the path is input/<subfolder>/<filename>; overwrite=true replaces it,
    otherwise an existing different file makes it "<stem> (n)<ext>"."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.workflows: dict[str, dict] = {}

    def upload(self, req: httpx.Request) -> httpx.Response:
        from email.parser import BytesParser
        from email.policy import default as email_policy

        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n"
            + req.content
        )
        fields: dict = {}
        upload: tuple[str, bytes] | None = None
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                upload = (part.get_filename(), part.get_payload(decode=True))
            else:
                fields[name] = part.get_payload(decode=True).decode()
        assert upload is not None
        filename, data = upload
        sub = fields.get("subfolder", "")
        key = f"{sub}/{filename}" if sub else filename
        if fields.get("overwrite") not in ("true", "1"):
            stem, dot, ext = filename.rpartition(".")
            n = 1
            while key in self.files and self.files[key] != data:
                filename = f"{stem} ({n}).{ext}" if dot else f"{filename} ({n})"
                key = f"{sub}/{filename}" if sub else filename
                n += 1
        self.files[key] = data
        return httpx.Response(200, json={"name": filename, "subfolder": sub, "type": "input"})


def test_b7r2_h3_same_basename_uploads_never_cross_jobs(tmp_path, monkeypatch):
    """B7-r2 H3 (review P07): jobs A and B upload different bytes under the
    same local basenames (take.png / mask.png). A stalls between its uploads
    and its submit while B uploads and submits. Each workflow must still
    load ITS OWN bytes from the provider store."""
    model = next(
        m
        for m in json.loads((SEEDS / "comfyui.json").read_text())["models"]
        if m["model_id"] == "comfyui.inpaint-sd15"
    )
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    monkeypatch.setenv("COMFYUI_URL", "http://comfy.test:8188/prompt")
    store = _ComfyStore()
    inputs = {}
    for job, shade in (("A", 0), ("B", 255)):
        d = tmp_path / f"project-{job}" / "masks"
        d.mkdir(parents=True)
        (d / "take.png").write_bytes(_png((shade, 1, 2)))
        (d / "mask.png").write_bytes(_png((shade, shade, shade)))
        inputs[job] = {
            "prompt": f"fix {job}", "negative_prompt": "", "seed": 1,
            "width": 8, "height": 8,
            "init_image": str(d / "take.png"), "mask": str(d / "mask.png"),
        }
    adapters: dict = {}

    def transport_for(job: str):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/upload/image":
                return store.upload(req)
            if req.url.path == "/prompt":
                if job == "A" and "B" not in store.workflows:
                    # Barrier: A has uploaded; B runs completely first.
                    adapters["B"].submit("comfyui.inpaint-sd15", inputs["B"])
                store.workflows[job] = json.loads(req.content)["prompt"]
                return httpx.Response(200, json={"prompt_id": f"p-{job}"})
            raise AssertionError(f"unexpected {req.method} {req.url}")

        return httpx.MockTransport(handler)

    adapters["A"] = HttpPollingAdapter(transport=transport_for("A"))
    adapters["B"] = HttpPollingAdapter(transport=transport_for("B"))
    adapters["A"].submit("comfyui.inpaint-sd15", inputs["A"])

    for job in ("A", "B"):
        wf = store.workflows[job]
        image_name, mask_name = wf["4"]["inputs"]["image"], wf["5"]["inputs"]["image"]
        assert store.files[image_name] == Path(inputs[job]["init_image"]).read_bytes(), job
        assert store.files[mask_name] == Path(inputs[job]["mask"]).read_bytes(), job
    names = {store.workflows[j]["5"]["inputs"]["image"] for j in ("A", "B")}
    assert len(names) == 2  # per-job names, not the shared local basename


def test_b7r2_h3_workflow_uses_the_name_the_server_returns(tmp_path, monkeypatch):
    """The workflow references the stored name the provider RETURNS (with
    its subfolder), never the local basename."""
    model = next(
        m
        for m in json.loads((SEEDS / "comfyui.json").read_text())["models"]
        if m["model_id"] == "comfyui.i2i-sd15"
    )
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    monkeypatch.setenv("COMFYUI_URL", "http://comfy.test:8188/prompt")
    src = tmp_path / "src.png"
    src.write_bytes(_png())
    sent: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/upload/image":
            sent["upload"] = req.content
            return httpx.Response(
                200, json={"name": "renamed (3).png", "subfolder": "hs", "type": "input"}
            )
        sent["workflow"] = json.loads(req.content)["prompt"]
        return httpx.Response(200, json={"prompt_id": "p1"})

    HttpPollingAdapter(transport=httpx.MockTransport(handler)).submit(
        "comfyui.i2i-sd15", {"prompt": "x", "seed": 1, "init_image": str(src)}
    )
    assert sent["workflow"]["4"]["inputs"]["image"] == "hs/renamed (3).png"
    assert b'filename="hs-' in sent["upload"] and b"-src.png" in sent["upload"]
    assert b'name="overwrite"\r\n\r\nfalse' in sent["upload"]


# --- M1: provider text in error_message is redacted -------------------------


def test_b7r2_m1_http_200_failed_error_text_is_redacted(env, monkeypatch):
    monkeypatch.setenv("FAKE_REF_KEY", "env-secret-value-4242")
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["model_id"] = "fake.failing-t2i"
    st = model["capabilities"]["http"]["status"]
    st["state_map"]["FAILED"] = "failed"
    st["error_path"] = "error"
    _register_model(monkeypatch, model)
    leaked = (
        "upstream rejected Authorization: Bearer sk-live-PROBE0123456789abcdef "
        'echo {"api_key": "probe-secret-777"} env-secret-value-4242 '
    ) + "z" * 5000

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "j1"})
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"state": "FAILED", "error": leaked})

    _install_http(monkeypatch, handler)
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.failing-t2i",
            prompt_final="x",
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "failed"
    message = row.error_message
    for secret in ("sk-live-PROBE0123456789abcdef", "probe-secret-777", "env-secret-value-4242"):
        assert secret not in message
    assert "[REDACTED]" in message
    assert len(message) <= defaults.HTTP_ERROR_SNIPPET_CHARS + 100  # capped


def test_b7r2_m1_unrecognized_state_text_is_redacted(monkeypatch):
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: PLAIN_MODEL)

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(200, json={"state": "token=sk-live-STATE0123456789xyz"})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit(
        "fake.plain-t2i",
        {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
    )
    with pytest.raises(StudioError) as ei:
        adapter.poll(pid)
    assert "STATE0123456789xyz" not in ei.value.message
    assert "STATE0123456789xyz" not in json.dumps(ei.value.details)


# --- M2: inline results are capped before they are read / decoded ------------


def test_b7r2_m2_inline_result_rejected_before_decoding(monkeypatch):
    """The encoded length is checked against the media cap BEFORE any
    base64 decode (no decoded allocation of an oversized result)."""
    monkeypatch.setattr(defaults, "HTTP_RESULT_MAX_BYTES_IMAGE", 64)
    big = base64.b64encode(_png() + b"\0" * 8200).decode()

    def no_decode(*a, **k):
        raise AssertionError("base64 decode must not run for an oversized result")

    monkeypatch.setattr(http_polling.base64, "b64decode", no_decode)
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, None, inline=big)
    assert ei.value.details["reason"] == "result_too_large"


def test_b7r2_m2_json_response_capped_while_streaming(monkeypatch):
    """A status/result JSON body is capped while it is received: the read
    stops mid-stream, the rest is never consumed."""
    monkeypatch.setattr(defaults, "HTTP_RESULT_MAX_BYTES_IMAGE", 64)
    monkeypatch.setattr(defaults, "HTTP_JSON_MAX_BYTES", 1024)
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["capabilities"]["http"]["result"] = {"output_bytes_path": "b64"}
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    payload = json.dumps(
        {"state": "DONE", "b64": base64.b64encode(b"\0" * 150_000).decode()}
    ).encode()
    chunks = [payload[i : i + 4096] for i in range(0, len(payload), 4096)]
    bodies: list[_Body] = []

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "j1"})
        body = _Body(list(chunks))
        bodies.append(body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit(
        "fake.plain-t2i",
        {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
    )
    with pytest.raises(StudioError) as ei:
        adapter.fetch_result(pid)
    assert ei.value.details["reason"] == "response_too_large"
    assert bodies and bodies[0].read < len(payload)  # stopped mid-stream


# --- M3: images must decode before they are published ------------------------


def test_b7r2_m3_signature_only_png_fails_the_job(env, monkeypatch):
    """Review P04: 8 bytes of PNG signature are not an image. The worker
    decodes every image result (any adapter) before publishing it."""

    class _BrokenPng(_CapturingAdapter):
        def fetch_result(self, provider_job_id):
            return b"\x89PNG\r\n\x1a\n" + b"not an image"

    monkeypatch.setitem(registry._ADAPTERS, "stub", _BrokenPng())
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="stub.t2i-fast",
            prompt_final="x",
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "failed", (row.status, row.output_path)
    assert row.error_code == ErrorCode.PROVIDER_ERROR
    assert "decoded" in row.error_message
    assert row.output_path is None
    frames = env["tmp"] / "projects" / str(env["project_id"]) / "frames"
    assert not frames.exists() or not any(frames.iterdir())  # nothing written


def test_b7r2_m3_http_adapter_rejects_undecodable_image(monkeypatch):
    sig_only = httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"\x89PNG\r\n\x1a\nxx"
    )
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, sig_only)
    assert ei.value.details["reason"] == "result_not_decodable"
    truncated = _png()[:-20]
    with pytest.raises(StudioError) as ei:
        _fetch(monkeypatch, None, inline=_data_uri(truncated))
    assert ei.value.details["reason"] == "result_not_decodable"


# --- M4: the executed config is the pre-check's config, and it is recorded ----


def _variant_model(variant: str) -> dict:
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["model_id"] = "fake.variant-t2i"
    model["capabilities"]["http"]["submit"]["body_template"]["variant"] = variant
    return model


def _set_db_caps(model: dict) -> None:
    with session_scope() as db:
        db.get(Model, model["model_id"]).capabilities = model["capabilities"]


def _variant_generation(env) -> str:
    from app.services import provider_input

    with session_scope() as db:
        snap = provider_input.image_snapshot(
            db, model_id="fake.variant-t2i", task="text_to_image",
            seed=3, seed_source="explicit",
        )
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.variant-t2i",
            prompt_final="x",
            params={"snapshot": snap},
            seed=3,
        )
        db.add(gen)
        db.flush()
        return str(gen.id)


def test_b7r2_m4_adapter_executes_the_db_overlay_config(env, monkeypatch):
    """The enqueue pre-check/snapshot read the DB row; the adapter used to
    read the seed cache. Both now see the same entry."""
    from app.models_lib import capabilities

    monkeypatch.setenv("FAKE_REF_KEY", "k")
    _register_model(monkeypatch, _variant_model("SEED"))  # seed cache + DB row
    db_model = _variant_model("DB")
    _set_db_caps(db_model)  # operator edit: DB overlay differs from the seed
    provider = _RefProvider()
    _install_http(monkeypatch, provider)
    gid = _variant_generation(env)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "succeeded", (row.error_code, row.error_message)
    assert provider.submits[0]["variant"] == "DB"
    executed = row.params["executed"]
    assert executed["model_config_sha256"] == capabilities.config_hash(db_model)
    assert executed["config_matches_snapshot"] is True
    assert row.params["snapshot"]["model_config_sha256"] == executed["model_config_sha256"]


def test_b7r2_m4_config_changed_after_enqueue_is_recorded_not_hidden(env, monkeypatch):
    """Policy (ADR-0014 B7-r2): the current config executes; the drift from
    the enqueue snapshot is recorded next to it (hash + sampler)."""
    from app.models_lib import capabilities

    monkeypatch.setenv("FAKE_REF_KEY", "k")
    _register_model(monkeypatch, _variant_model("A"))
    gid = _variant_generation(env)  # snapshot taken with config A
    snap_hash = _get(Generation, gid).params["snapshot"]["model_config_sha256"]
    changed = _variant_model("B")
    _set_db_caps(changed)  # config changes while the job is queued
    provider = _RefProvider()
    _install_http(monkeypatch, provider)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "succeeded", (row.error_code, row.error_message)
    assert provider.submits[0]["variant"] == "B"
    executed = row.params["executed"]
    assert executed["model_config_sha256"] == capabilities.config_hash(changed)
    assert executed["model_config_sha256"] != snap_hash
    assert executed["snapshot_model_config_sha256"] == snap_hash
    assert executed["config_matches_snapshot"] is False
    assert "sampler" in executed
    assert row.params["snapshot"]["model_config_sha256"] == snap_hash  # untouched


# --- L1: explicit empty tasks ------------------------------------------------


def test_b7r2_l1_explicit_empty_tasks_supports_nothing():
    from app.models_lib import capabilities

    declared_none = {"model_id": "m", "capabilities": {"tasks": []}}
    assert capabilities.declared_tasks(declared_none) == []
    for task in ("text_to_image", "thumbnail", "inpaint"):
        assert capabilities.supports_task(declared_none, task) is False
    with pytest.raises(StudioError) as ei:
        capabilities.check_request(declared_none, "text_to_image")
    assert ei.value.details["reason"] == "task_unsupported"
    # Key absent (or null) = no declaration: legacy non-editing behavior.
    for legacy in ({"capabilities": {}}, {"capabilities": {"tasks": None}}, {}):
        assert capabilities.declared_tasks(legacy) is None
        assert capabilities.supports_task(legacy, "text_to_image") is True
        assert capabilities.supports_task(legacy, "inpaint") is False


# ---------------------------------------------------------------------------
# B7-r3: request-side deadline (H1), redaction before truncation and of
# metadata (M1), redirect/compression receive caps (M2), multi-frame images
# (M3), CLI descriptors (M4), remote id collisions (M5)
# ---------------------------------------------------------------------------


class _SlowReaderServer:
    """Real localhost HTTP server (127.0.0.1) that reads the REQUEST BODY
    slowly (64 KiB every 20 ms, small receive buffer), then answers
    ``{"id": "j1"}``. A large request body cannot be sent faster than that."""

    READ = 64 * 1024
    INTERVAL = 0.02

    def __init__(self):
        import socket
        import threading

        self.requests: list[str] = []
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        import socket
        import threading

        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            header, _, body = head.partition(b"\r\n\r\n")
            self.requests.append(header.split(b"\r\n", 1)[0].decode())
            length = 0
            for line in header.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            got = len(body)
            while got < length and not self._stop.is_set():
                chunk = conn.recv(min(self.READ, length - got))
                if not chunk:
                    return
                got += len(chunk)
                time.sleep(self.INTERVAL)
            reply = b'{"id": "j1", "state": "QUEUED"}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                b"content-length: " + str(len(reply)).encode() + b"\r\n\r\n" + reply
            )
        except OSError:
            pass  # the client gave up (that is the point)
        finally:
            conn.close()

    def close(self):
        self._stop.set()
        self.sock.close()


@pytest.fixture()
def slow_reader(monkeypatch):
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    server = _SlowReaderServer()
    yield server
    server.close()


_BIG_PROMPT = "p" * (12 * 1024 * 1024)  # one ~12 MiB JSON request chunk


def test_b7r3_h1_slow_reader_cannot_stretch_a_large_submit_past_the_deadline(
    slow_reader, monkeypatch
):
    """Review P01: httpcore re-uses one timeout for every partial send of a
    write; a reader draining slowly lets a large body (one chunk) take far
    longer than the deadline. Each send must get the time left."""
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", _DRIP_DEADLINE)
    model = _drip_model(slow_reader.base, submit="slow", status="ok", cancel="ok")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    adapter = HttpPollingAdapter()  # real transport
    started = time.monotonic()
    with pytest.raises(StudioError) as ei:
        adapter.submit(
            "fake.drip-t2i",
            {"prompt": _BIG_PROMPT, "negative_prompt": "", "seed": 1, "width": 8, "height": 8},
        )
    elapsed = time.monotonic() - started
    assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
    assert elapsed < _DRIP_DEADLINE + _DRIP_MARGIN, f"submit took {elapsed:.2f}s"
    assert len(slow_reader.requests) == 1


def test_b7r3_h1_slow_reader_submit_fails_uncertain_never_resubmitted(
    env, slow_reader, monkeypatch
):
    """D7 end to end: the request was (partly) sent, its outcome unknown ->
    failed + PROVIDER_UNCERTAIN, one submit, no automatic retry."""
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", _DRIP_DEADLINE)
    model = _drip_model(slow_reader.base, submit="slow", status="ok", cancel="ok")
    _register_model(monkeypatch, model)
    monkeypatch.setitem(registry._ADAPTERS, "http_polling", HttpPollingAdapter())
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.drip-t2i",
            prompt_final=_BIG_PROMPT,
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    started = time.monotonic()
    tasks_image.run_generation.apply(args=[gid])
    elapsed = time.monotonic() - started
    row = _get(Generation, gid)
    assert row.status == "failed"
    assert row.error_code == ErrorCode.PROVIDER_UNCERTAIN, row.error_message
    assert "deadline" in row.error_message
    assert row.retry_count == 0
    assert len(slow_reader.requests) == 1  # never resubmitted
    assert elapsed < _DRIP_DEADLINE + 3.0


# --- M1: redaction happens before any cut; metadata is redacted too -----------


def _cli_failure_message(tmp_path, stderr: str) -> str:
    from app.models_lib.adapters import cli as cli_mod

    err = tmp_path / "stderr.log"
    err.write_text(stderr)
    job = cli_mod._CliJob(
        model_id="codex.t2i-gptimage2", tool="codex", command="codex",
        proc=None, out_dir=tmp_path, stderr_path=err,
    )
    status = CliAdapter()._finish(job, "job-x", 1)
    assert status.state == "failed"
    return status.error_message


def test_b7r3_m1_cli_credential_at_the_cut_boundary_is_redacted(tmp_path):
    """Review P03: 'Authorization: ' + 490 chars at the end of stderr. Cutting
    to 500 chars first removed the 'Autho' prefix, so the value survived."""
    credential = "Q" * 490
    message = _cli_failure_message(
        tmp_path, "starting\n" + "Authorization: " + credential + "\n"
    )
    assert credential[:40] not in message
    assert "[REDACTED]" in message


def test_b7r3_m1_cli_credential_cut_by_the_read_window_is_not_quoted(tmp_path):
    """A credential line longer than the read window: its first part (with
    the 'Authorization:' prefix) is outside the window. The fragment is
    dropped, never quoted."""
    credential = "B" * (70 * 1024)
    message = _cli_failure_message(
        tmp_path, "Authorization: " + credential + "\nexit: model refused\n"
    )
    assert "BBBBBBBBBB" not in message
    assert "model refused" in message


def test_b7r3_m1_known_secret_echoed_as_job_id_never_reaches_error_message(
    env, monkeypatch
):
    """Review P04: the provider returns our key as its job id, then the
    status call fails with 500 — the id quoted in the message is redacted."""
    secret = "Zsecret-value-9876543210"
    monkeypatch.setenv("FAKE_REF_KEY", secret)
    _register_model(monkeypatch, PLAIN_MODEL)

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": secret})
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(500, text="status backend down")

    _install_http(monkeypatch, handler)
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"],
            job_type="text_to_image",
            model_id="fake.plain-t2i",
            prompt_final="x",
            params={},
            seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "failed"
    assert "HTTP 500" in row.error_message
    assert secret not in row.error_message
    assert "[REDACTED]" in row.error_message


# --- M2: redirects and compressed bodies stay under the receive caps -----------


def _plain_adapter(monkeypatch, handler) -> HttpPollingAdapter:
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: PLAIN_MODEL)
    return HttpPollingAdapter(transport=httpx.MockTransport(handler))


_PLAIN_PAYLOAD = {"prompt": "x", "negative_prompt": "", "seed": 1, "width": 8, "height": 8}


def test_b7r3_m2_redirect_body_is_never_read(monkeypatch):
    """Review P02: httpx read a 302's whole body (256 KiB) outside the capped
    loop. The adapter follows redirects itself and closes their bodies
    unread."""
    monkeypatch.setattr(defaults, "HTTP_JSON_MAX_BYTES", 64)
    redirect_body = _Body([b"r" * 4096] * 64)  # 256 KiB

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(
                302, headers={"location": "https://ref.test/v1/generate-2"},
                stream=redirect_body,
            )
        if req.url.path == "/v1/generate-2":
            return httpx.Response(200, json={"id": "j1"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    pid = _plain_adapter(monkeypatch, handler).submit("fake.plain-t2i", _PLAIN_PAYLOAD)
    assert redirect_body.read == 0  # nothing past the cap; nothing at all
    assert pid.startswith("j1")


def test_b7r3_m2_redirect_loop_is_bounded(monkeypatch):
    hops: list[str] = []

    def handler(req):
        hops.append(req.url.path)
        return httpx.Response(302, headers={"location": f"/v1/hop-{len(hops)}"})

    with pytest.raises(StudioError) as ei:
        _plain_adapter(monkeypatch, handler).submit("fake.plain-t2i", _PLAIN_PAYLOAD)
    assert ei.value.details.get("reason") == "too_many_redirects"
    assert len(hops) <= 6


def test_b7r3_m2_gzip_bomb_rejected_before_decompression(monkeypatch):
    """Review P06: the decoder inflated ~100 bytes on the wire to 1 MiB
    before the cap saw it. Compressed bodies are refused (identity only)."""
    import gzip
    import tracemalloc

    monkeypatch.setattr(defaults, "HTTP_JSON_MAX_BYTES", 4096)
    bomb = gzip.compress(b"0" * (8 * 1024 * 1024))
    seen: dict = {}

    def handler(req):
        seen["accept-encoding"] = req.headers.get("accept-encoding")
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=_Body([bomb]),
        )

    adapter = _plain_adapter(monkeypatch, handler)
    tracemalloc.start()
    try:
        with pytest.raises(StudioError) as ei:
            adapter.submit("fake.plain-t2i", _PLAIN_PAYLOAD)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert ei.value.details.get("reason") == "response_encoding"
    assert peak < 1024 * 1024, f"peak allocation {peak} bytes"
    assert seen["accept-encoding"] == "identity"


# --- M3: only single-frame still images ----------------------------------------


def _gif(frames: int) -> bytes:
    images = [Image.new("RGB", (16, 16), (40 * i, 10, 10)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=frames > 1, append_images=images[1:])
    return buf.getvalue()


def test_b7r3_m3_gif_with_truncated_second_frame_is_rejected():
    """Review P07: load() decoded only the first frame, so a GIF whose second
    frame is cut off passed. Multi-frame images are rejected outright."""
    from app.models_lib import media_check

    two = _gif(2)
    truncated = two[: len(two) - 12]  # second frame's data cut off
    for data in (truncated, two):
        with pytest.raises(StudioError) as ei:
            media_check.check_media(data, "image", what="probe")
        assert ei.value.details["reason"] in ("result_multi_frame", "result_not_decodable")
    with pytest.raises(StudioError) as ei:
        media_check.check_media(two, "image", what="probe")
    assert ei.value.details == {"reason": "result_multi_frame", "frames": 2}
    media_check.check_media(_gif(1), "image", what="probe")  # still GIF: fine


# --- M4: CLI submits leave no descriptor behind -------------------------------


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_b7r3_m4_cli_submits_do_not_leak_descriptors(tmp_path, monkeypatch):
    """Review P08: each submit left the two mkstemp descriptors open."""
    from unittest import mock

    class _Proc:
        pid = 1

        def __init__(self, argv, **kw):
            pass  # keeps no reference to the passed file objects

        def poll(self):
            return None

    model = _cli_model("codex", str(tmp_path))
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    with (
        mock.patch("app.models_lib.adapters.cli.shutil.which", return_value="/bin/codex"),
        mock.patch.object(CliAdapter, "_check_login", lambda *a: None),
        mock.patch("app.models_lib.adapters.cli.subprocess.Popen", _Proc),
    ):
        adapter = CliAdapter()
        adapter.submit("codex.t2i-gptimage2", {"prompt": "warm-up"})
        before = _open_fds()
        for i in range(10):
            adapter.submit("codex.t2i-gptimage2", {"prompt": f"scene {i}"})
        after = _open_fds()
    assert after - before <= 0, f"{after - before} descriptors leaked by 10 submits"
    job = next(iter(adapter._jobs.values()))
    assert job.stderr_path.parent == job.out_dir  # logs live in the job dir


# --- M5: two endpoints, same remote id ----------------------------------------


def _endpoint_model(host: str) -> dict:
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["model_id"] = f"fake.{host}-t2i"
    http = model["capabilities"]["http"]
    http["submit"]["url"] = f"https://{host}.test/v1/generate"
    http["status"]["url_template"] = f"https://{host}.test/v1/jobs/{{job_id}}"
    http["status"]["state_map"]["QUEUED"] = "queued"
    http["result"] = {"output_url_path": "url"}
    http["cancel"] = {"url_template": f"https://{host}.test/v1/jobs/{{job_id}}/cancel"}
    return model


def test_b7r3_m5_same_remote_id_from_two_endpoints_never_collides(monkeypatch):
    """Review P05: endpoints A and B both answer id "1"; the singleton
    adapter keyed its state by that id, so A's poll/fetch hit B. State is
    keyed by a local handle; each job reaches its own endpoint and image."""
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    models = {m["model_id"]: m for m in (_endpoint_model("a"), _endpoint_model("b"))}
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: models[mid])
    images = {"a": _png((255, 0, 0)), "b": _png((0, 0, 255))}
    hits: list[str] = []

    def handler(req):
        host = req.url.host.split(".")[0]
        hits.append(f"{req.method} {host} {req.url.path}")
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "1"})
        if req.url.path == "/v1/jobs/1":
            return httpx.Response(
                200, json={"state": "DONE", "url": f"https://{host}.test/out.png"}
            )
        if req.url.path == "/out.png":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=images[host])
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    handle_a = adapter.submit("fake.a-t2i", _PLAIN_PAYLOAD)
    handle_b = adapter.submit("fake.b-t2i", _PLAIN_PAYLOAD)
    assert handle_a != handle_b
    hits.clear()
    assert adapter.poll(handle_a).state == "succeeded"
    assert hits == ["GET a /v1/jobs/1"]
    assert adapter.fetch_result(handle_a) == images["a"]
    assert adapter.fetch_result(handle_b) == images["b"]
    assert http_polling.remote_job_id(handle_a) == "1"

    # The persisted handle still works in another process (restart): the
    # entry is rebuilt from the registry; poll/fetch/cancel reach endpoint B.
    fresh = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    hits.clear()
    assert fresh.poll(handle_b).state == "succeeded"
    assert fresh.fetch_result(handle_b) == images["b"]
    fresh.cancel(handle_b)
    assert hits[0] == "GET b /v1/jobs/1" and "POST b /v1/jobs/1/cancel" in hits
    # A bare remote id persisted before B7-r3 stays what it was: unknown to a
    # fresh process (poll -> MODEL_UNAVAILABLE, cancel -> best-effort no-op).
    with pytest.raises(StudioError) as ei:
        fresh.poll("1")
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert fresh.cancel("1") is None


def test_b7r3_review_m1_deadline_error_never_quotes_a_cut_secret(env, monkeypatch):
    """Review B7R3-M1: a long secret reflected as the provider job id must not
    survive in the deadline error. Truncating the id to the quote cap BEFORE
    redaction left its first 120 characters, which exact-match redaction no
    longer recognised."""
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic, raising=False)
    monkeypatch.setattr(http_polling, "_sleep", clock.sleep, raising=False)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 30.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_MAX_SEC", 0.0)
    monkeypatch.setattr(defaults, "POLL_MAX_ITERATIONS", 50)
    secret = "Zlong-secret-" + "q7" * 100  # 213 chars: crosses the 120-char quote cap
    monkeypatch.setenv("FAKE_REF_KEY", secret)
    _register_model(monkeypatch, PLAIN_MODEL)

    def handler(req):
        clock.now += 5.0  # each provider request "takes" 5s
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": secret})
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"state": "QUEUED"})

    _install_http(monkeypatch, handler)
    with session_scope() as db:
        gen = Generation(
            project_id=env["project_id"], job_type="text_to_image",
            model_id="fake.plain-t2i", prompt_final="x", params={}, seed=1,
        )
        db.add(gen)
        db.flush()
        gid = str(gen.id)
    tasks_image.run_generation.apply(args=[gid])
    row = _get(Generation, gid)
    assert row.status == "failed"
    assert "deadline" in row.error_message
    # No 24-char window of the secret may appear anywhere in the message.
    leaked = [secret[i:i + 24] for i in range(0, len(secret) - 24) if secret[i:i + 24] in row.error_message]
    assert leaked == [], row.error_message


# ---------------------------------------------------------------------------
# B9a: restored handles (M2 deadline, M3 config), connect bound per address +
# DNS (M4), configured headers on cross-origin redirects (M5)
# ---------------------------------------------------------------------------


class _ClockDrip(httpx.SyncByteStream):
    """A body whose every chunk "takes" ``step`` seconds on the fake clock."""

    def __init__(self, clock, data: bytes, *, size: int, step: float):
        self.clock, self.data, self.size, self.step = clock, data, size, step
        self.read = 0

    def __iter__(self):
        for start in range(0, len(self.data), self.size):
            self.clock.now += self.step
            self.read += 1
            yield self.data[start : start + self.size]


def _restore_setup(monkeypatch, clock, deadline: float) -> None:
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic)
    monkeypatch.setattr(http_polling, "_sleep", clock.sleep)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", deadline)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_MAX_SEC", 0.0)
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: PLAIN_MODEL)


def test_b9_m2_restored_handle_poll_is_bounded_by_one_new_deadline(monkeypatch):
    """Review B9 M2: a fresh adapter restoring a persisted handle kept no
    deadline, so poll_until_done's local deadline never reached the HTTP
    layer and a dripping status body ran far past it. The restore stores ONE
    new total deadline that deadline_for() and the HTTP calls share."""
    clock = _Clock()
    _restore_setup(monkeypatch, clock, 30.0)
    cancels: list[str] = []

    def handler(req):
        if req.url.path.endswith("/cancel"):
            cancels.append(req.url.path)
            return httpx.Response(200, json={})
        if req.url.path == "/v1/jobs/j1":
            body = b'{"state": "QUEUED", "pad": "' + b"x" * 40 + b'"}'
            return httpx.Response(200, stream=_ClockDrip(clock, body, size=1, step=5.0))
        raise AssertionError(f"unexpected {req.method} {req.url}")

    handle = http_polling.job_handle("j1", "fake.plain-t2i")
    fresh = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    started = clock.now
    with pytest.raises(StudioError) as ei:
        poll_until_done(fresh, handle)
    assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
    # Stopped at the new deadline (one 5 s chunk of slack), not after the
    # whole ~350 s drip.
    assert clock.now - started <= 30.0 + 5.0, clock.now - started
    assert fresh.deadline_for(handle) == pytest.approx(started + 30.0)
    assert cancels == ["/v1/jobs/j1/cancel"]


def test_b9_m2_restored_handle_result_fetch_shares_the_same_deadline(monkeypatch):
    """The deadline set at restore also bounds fetch_result (status + a
    dripping download); it is not restarted per call."""
    clock = _Clock()
    _restore_setup(monkeypatch, clock, 30.0)
    image = _png(size=(32, 32))

    def handler(req):
        if req.url.path == "/v1/jobs/j1":
            return httpx.Response(
                200, json={"state": "DONE", "url": "https://cdn.ref.test/out.png"}
            )
        if req.url.host == "cdn.ref.test":
            return httpx.Response(
                200, headers={"content-type": "image/png"},
                stream=_ClockDrip(clock, image, size=4, step=5.0),
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    handle = http_polling.job_handle("j1", "fake.plain-t2i")
    fresh = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    started = clock.now
    assert fresh.poll(handle).state == "succeeded"
    deadline = fresh.deadline_for(handle)
    assert deadline == pytest.approx(started + 30.0)
    clock.now += 20.0  # time passes between the poll and the result fetch
    with pytest.raises(StudioError) as ei:
        fresh.fetch_result(handle)
    assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
    assert fresh.deadline_for(handle) == deadline  # one deadline, not restarted
    assert clock.now - started <= 30.0 + 5.0, clock.now - started


def _moved_model(host: str) -> dict:
    model = _endpoint_model(host)
    model["model_id"] = "fake.moved-t2i"
    return model


def test_b9_m3_restore_uses_the_db_overlay_not_the_seed(env, monkeypatch):
    """Review B9 M3: the restore path read the SEED config while the worker
    pins the DB overlay. Seed endpoint A, DB row endpoint B: a restored
    handle must poll B (implicitly and through the explicit API), and a
    DB that cannot be read is an error, never a silent seed fallback."""
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    _register_model(monkeypatch, _moved_model("a"))  # seed cache + DB row (A)
    _set_db_caps(_moved_model("b"))  # operator edit: the DB row now says B
    hits: list[str] = []

    def handler(req):
        hits.append(f"{req.method} {req.url.host.split('.')[0]} {req.url.path}")
        return httpx.Response(200, json={"state": "QUEUED"})

    handle = http_polling.job_handle("1", "fake.moved-t2i")
    assert HttpPollingAdapter(transport=httpx.MockTransport(handler)).poll(handle).state == "queued"
    assert hits == ["GET b /v1/jobs/1"]

    # Explicit API: the caller passes the entry it resolved (worker: DB overlay).
    hits.clear()
    explicit = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    with session_scope() as db:
        entry = registry.get_model("fake.moved-t2i", db)
    explicit.restore(handle, entry)
    assert explicit.deadline_for(handle) is not None
    assert explicit.poll(handle).state == "queued"
    assert hits == ["GET b /v1/jobs/1"]
    with pytest.raises(StudioError) as ei:  # entry of another model: refused
        HttpPollingAdapter(transport=httpx.MockTransport(handler)).restore(
            handle, _endpoint_model("b")
        )
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE

    # DB unreadable -> MODEL_UNAVAILABLE, no request to the seed endpoint.
    hits.clear()

    def _broken():
        raise RuntimeError("database is down")

    monkeypatch.setattr(sess_mod, "get_session_factory", _broken)
    with pytest.raises(StudioError) as ei:
        HttpPollingAdapter(transport=httpx.MockTransport(handler)).poll(handle)
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE, ei.value.message
    assert hits == []


class _FakeConnectSockets:
    """Patches socket.getaddrinfo / socket.socket so the REAL httpx transport
    (httpcore SyncBackend -> socket.create_connection) runs against
    controlled sockets: no packet leaves the process."""

    def __init__(
        self, monkeypatch, *, names: dict, connect, getaddrinfo_hook=None, infos=None
    ):
        import socket as socket_mod

        self.attempts: list[tuple[str, float | None]] = []
        # (socket family, full sockaddr) of every connect (B9a-r2 M3).
        self.sockaddrs: list[tuple[int, tuple]] = []
        outer = self

        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if getaddrinfo_hook is not None:
                getaddrinfo_hook(host)
            if infos is not None and host in infos:
                return list(infos[host])  # raw addrinfo tuples (e.g. AF_INET6)
            addresses = names.get(host, [host])
            return [
                (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (a, port))
                for a in addresses
            ]

        class FakeSocket:
            def __init__(self, family=-1, *a, **k):
                self.family = family
                self.timeout = None

            def settimeout(self, value):
                self.timeout = value

            def setsockopt(self, *a):
                pass

            def bind(self, *a):
                pass

            def close(self):
                pass

            def connect(self, sockaddr):
                outer.sockaddrs.append((self.family, tuple(sockaddr)))
                outer.attempts.append((sockaddr[0], self.timeout))
                connect(sockaddr[0], self.timeout)

        monkeypatch.setattr(socket_mod, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(socket_mod, "socket", FakeSocket)


def _socket_model(host: str) -> dict:
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["model_id"] = "fake.sock-t2i"
    http = model["capabilities"]["http"]
    http["submit"]["url"] = f"http://{host}:8080/v1/generate"
    http["submit"]["headers"] = {}
    http["status"]["url_template"] = f"http://{host}:8080/v1/jobs/{{job_id}}"
    http["cancel"] = {"url_template": f"http://{host}:8080/v1/jobs/{{job_id}}/cancel"}
    return model


def test_b9_m4_connect_attempts_share_the_deadline_across_addresses(monkeypatch):
    """Review B9 M4: the connect timeout was computed once and the stdlib
    re-applied it per resolved address, so two addresses took ~2x the time
    left. The first address refuses after 4 s, the second blackholes: it must
    get only the 6 s left, and the call ends at the deadline."""
    import socket as socket_mod

    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 10.0)
    model = _socket_model("dual.test")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)

    def connect(address, timeout):
        if address == "10.255.0.1":
            clock.now += 4.0
            raise ConnectionRefusedError(61, "refused")
        clock.now += timeout  # blackhole: the whole timeout passes
        raise socket_mod.timeout("timed out")

    fake = _FakeConnectSockets(
        monkeypatch, names={"dual.test": ["10.255.0.1", "10.255.0.2"]}, connect=connect
    )
    adapter = HttpPollingAdapter()  # real transport, controlled sockets
    started = clock.now
    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.sock-t2i", _PLAIN_PAYLOAD)
    assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
    assert [a for a, _ in fake.attempts] == ["10.255.0.1", "10.255.0.2"]
    assert [t for _, t in fake.attempts] == pytest.approx([10.0, 6.0])
    assert clock.now - started == pytest.approx(10.0)


def test_b9_m4_dns_resolution_is_bounded_for_calls_and_cancel(monkeypatch):
    """Review B9 M4: getaddrinfo had no bound. A resolver that hangs must
    not stretch a job call past its deadline, nor cancel past its cap."""
    import threading

    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 0.3)
    monkeypatch.setattr(defaults, "HTTP_CANCEL_TIMEOUT_SEC", 0.3)
    model = _socket_model("slowdns.test")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    release = threading.Event()

    def hang(host):
        if host == "slowdns.test":
            release.wait(5.0)

    def refuse(address, timeout):
        raise ConnectionRefusedError(61, "refused")

    _FakeConnectSockets(
        monkeypatch, names={"slowdns.test": ["10.255.0.9"]}, connect=refuse,
        getaddrinfo_hook=hang,
    )
    adapter = HttpPollingAdapter()
    try:
        started = time.monotonic()
        with pytest.raises(StudioError) as ei:
            adapter.submit("fake.sock-t2i", _PLAIN_PAYLOAD)
        elapsed = time.monotonic() - started
        assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
        assert elapsed < 0.3 + _DRIP_MARGIN, f"submit took {elapsed:.2f}s"

        started = time.monotonic()
        adapter.cancel(http_polling.job_handle("j1", "fake.sock-t2i"))  # best effort
        elapsed = time.monotonic() - started
        assert elapsed < 0.3 + _DRIP_MARGIN, f"cancel took {elapsed:.2f}s"
    finally:
        release.set()


def _header_model() -> dict:
    model = json.loads(json.dumps(PLAIN_MODEL))
    model["capabilities"]["http"]["submit"]["headers"] = {
        "Authorization": "env:FAKE_REF_KEY",
        "X-API-Key": "env:FAKE_REF_KEY",
        "X-Tenant": "studio",
    }
    return model


_CONFIGURED = ("authorization", "x-api-key", "x-tenant")


@pytest.mark.parametrize(
    "hops,kept",
    [
        (["/v1/next"], True),  # same origin
        (["https://other.test/v1/next"], False),  # host
        (["http://ref.test/v1/next"], False),  # scheme
        (["https://ref.test:8443/v1/next"], False),  # port
        (["https://other.test/v1/hop", "https://ref.test/v1/next"], False),  # back
    ],
    ids=["same-origin", "host", "scheme", "port", "back-to-origin"],
)
def test_b9_m5_cross_origin_redirect_drops_every_configured_header(
    monkeypatch, hops, kept
):
    """Review B9 M5: httpx strips only Authorization on a cross-origin
    redirect; X-API-Key-style auth headers from the seed config went to the
    other origin. Any cross-origin hop now drops every configured header
    (also on a later hop back); a same-origin hop keeps them."""
    monkeypatch.setenv("FAKE_REF_KEY", "secret-k")
    model = _header_model()
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    seen: dict[str, httpx.Headers] = {}
    chain = list(hops)

    def handler(req):
        seen[str(req.url)] = req.headers
        if req.url.path == "/v1/next":
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(307, headers={"location": chain.pop(0)})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    assert adapter.submit("fake.plain-t2i", _PLAIN_PAYLOAD).startswith("j1@")
    first = seen.pop("https://ref.test/v1/generate")
    assert all(name in first for name in _CONFIGURED)
    final = next(h for url, h in seen.items() if url.endswith("/v1/next"))
    for headers in seen.values():
        present = [name for name in _CONFIGURED if name in headers]
        if kept:
            assert present == list(_CONFIGURED)
        else:
            assert present == [], present
    assert final.get("content-type") == "application/json"  # body still framed


# ---------------------------------------------------------------------------
# B9a-r2 (Codex review of B9a): M1 seed framing values across origins, M2
# resolver thread cap, M3 IPv6 scope, M4 restore/cancel bounds start before
# the registry read, M5 explicit model id on a cached handle, M6 mismatch
# message never quotes the remote id
# ---------------------------------------------------------------------------


_CT_SECRET = "application/json; api_key=ct-secret-4242"


@pytest.mark.parametrize(
    "status,location,method,expected_ct",
    [
        (307, "https://other.test/v1/next", "POST", "application/json"),
        (303, "https://other.test/v1/next", "GET", None),
        (307, "/v1/next", "POST", _CT_SECRET),  # same origin: the seed's own value
    ],
    ids=["307-cross-resend", "303-cross-get", "307-same-origin"],
)
def test_b9a_r2_m1_seed_content_type_value_never_crosses_origins(
    monkeypatch, status, location, method, expected_ct
):
    """Review B9a-r2 M1: Content-Type was kept verbatim from the seed on a
    cross-origin hop, so an env: value carrying a credential went to the
    other origin. Across origins a seed-set framing header is re-derived
    from the body (as httpx generates it) or dropped when no body is
    re-sent; a same-origin hop is unchanged."""
    monkeypatch.setenv("FAKE_REF_KEY", "secret-k")
    monkeypatch.setenv("B9_CT", _CT_SECRET)
    model = _header_model()
    model["capabilities"]["http"]["submit"]["headers"]["Content-Type"] = "env:B9_CT"
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    seen: list[tuple[str, str, httpx.Headers, bytes]] = []

    def handler(req):
        seen.append((str(req.url), req.method, req.headers, req.content))
        if req.url.path == "/v1/next":
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(status, headers={"location": location})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    assert adapter.submit("fake.plain-t2i", _PLAIN_PAYLOAD).startswith("j1@")
    assert seen[0][2].get("content-type") == _CT_SECRET  # the origin gets the seed's
    _url, sent_method, headers, content = seen[1]
    assert sent_method == method
    assert headers.get("content-type") == expected_ct
    if expected_ct != _CT_SECRET:
        assert [v for v in headers.values() if "ct-secret" in v] == []
    if method == "POST":
        assert json.loads(content) == json.loads(seen[0][3])  # same body, re-framed
        assert headers.get("content-length") == str(len(content))
    else:
        assert content == b""


def test_b9a_r2_m1_seed_framed_multipart_is_not_resent_to_another_origin(monkeypatch):
    """A multipart body framed by a seed Content-Type (its boundary is seed
    data) cannot be re-derived: the cross-origin re-send is refused with a
    clear error and nothing reaches the other origin."""
    hosts: list[str] = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "other.test":
            return httpx.Response(200, json={"name": "x.png"})
        return httpx.Response(307, headers={"location": "https://other.test/upload"})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(StudioError) as ei:
        adapter._call(
            "POST", "https://ref.test/upload",
            headers={"Content-Type": "multipart/form-data; boundary=seed-b0undary"},
            data={"type": "input"}, files={"image": ("x.png", b"123", "image/png")},
            timeout=5.0, what="Input upload", deadline=None,
        )
    assert ei.value.details.get("reason") == "redirect_reframe_refused", ei.value.message
    assert hosts == ["ref.test"]


def _dns_threads() -> list:
    import threading

    return [t for t in threading.enumerate() if t.name == "http-polling-dns" and t.is_alive()]


def _until(predicate, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_b9a_r2_m2_resolver_threads_are_capped_process_wide(monkeypatch):
    """Review B9a-r2 M2: every DNS timeout left one resolver thread behind
    and nothing capped their number. With the cap at 2, five calls against a
    hanging resolver leave at most 2 threads; calls 3-5 wait for a slot
    inside their own deadline and fail with the deadline error. A slot is
    freed only when its resolver returns."""
    import threading

    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 0.2)
    monkeypatch.setattr(defaults, "HTTP_DNS_MAX_RESOLVER_THREADS", 2, raising=False)
    model = _socket_model("slowdns.test")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    release = threading.Event()
    lookups: list[str] = []

    def hang(host):
        if host == "slowdns.test":
            lookups.append(host)
            release.wait(10.0)

    def refuse(address, timeout):
        raise ConnectionRefusedError(61, "refused")

    _FakeConnectSockets(
        monkeypatch, names={"slowdns.test": ["10.255.0.9"]}, connect=refuse,
        getaddrinfo_hook=hang,
    )
    assert _until(lambda: not _dns_threads()), "resolvers of an earlier test still run"
    adapter = HttpPollingAdapter()
    try:
        for _ in range(5):
            started = time.monotonic()
            with pytest.raises(StudioError) as ei:
                adapter.submit("fake.sock-t2i", _PLAIN_PAYLOAD)
            elapsed = time.monotonic() - started
            assert ei.value.details.get("reason") == "deadline_exceeded", ei.value.message
            assert elapsed < 0.2 + _DRIP_MARGIN, f"submit took {elapsed:.2f}s"
        assert len(_dns_threads()) <= 2, _dns_threads()
        assert len(lookups) == 2
    finally:
        release.set()
    assert _until(lambda: not _dns_threads())
    lookups.clear()
    with pytest.raises(StudioError) as ei:  # slots free again: resolves, then refused
        adapter.submit("fake.sock-t2i", _PLAIN_PAYLOAD)
    assert lookups == ["slowdns.test"]
    assert ei.value.details.get("reason") != "deadline_exceeded", ei.value.message


def test_b9a_r2_m3_scoped_ipv6_address_keeps_its_scope_and_family(monkeypatch):
    """Review B9a-r2 M3: the resolved address was reduced to its IP string,
    so link-local ('fe80::1234', port, 0, 7) was connected on scope 0 (and
    re-resolved as whatever family the stdlib picked). Each attempt now uses
    the full sockaddr and family; de-duplication keeps the same IP on two
    interfaces apart."""
    import socket as socket_mod

    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 30.0)
    model = _socket_model("lan.test")
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: model)
    v6 = socket_mod.AF_INET6
    stream = socket_mod.SOCK_STREAM
    scoped7 = ("fe80::1234", 8080, 0, 7)
    scoped9 = ("fe80::1234", 8080, 0, 9)
    infos = {"lan.test": [
        (v6, stream, 6, "", scoped7),
        (v6, stream, 6, "", scoped9),  # same IP, another interface
        (v6, stream, 6, "", scoped7),  # duplicate
    ]}

    def refuse(address, timeout):
        raise ConnectionRefusedError(61, "refused")

    fake = _FakeConnectSockets(monkeypatch, names={}, connect=refuse, infos=infos)
    with pytest.raises(StudioError):
        HttpPollingAdapter().submit("fake.sock-t2i", _PLAIN_PAYLOAD)
    assert fake.sockaddrs == [(v6, scoped7), (v6, scoped9)]


def test_b9a_r2_m4_restore_and_cancel_bounds_start_before_the_registry_read(monkeypatch):
    """Review B9a-r2 M4: the job deadline (restore) and cancel's cap started
    only AFTER the registry/DB read, so a slow read was outside both bounds.
    Both now start at entry, and the read gets the time left as its bound."""
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 30.0)
    monkeypatch.setattr(defaults, "HTTP_CANCEL_TIMEOUT_SEC", 10.0)
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    budgets: list[float | None] = []
    delay = {"sec": 8.0}

    def slow_resolve(model_id, timeout=None):
        budgets.append(timeout)
        clock.now += delay["sec"]  # the registry read "takes" this long
        return PLAIN_MODEL

    monkeypatch.setattr(registry, "resolve_model", slow_resolve)
    cancel_timeouts: list[float] = []

    def handler(req):
        cancel_timeouts.append(req.extensions["timeout"]["connect"])
        return httpx.Response(200, json={})

    handle = http_polling.job_handle("j1", "fake.plain-t2i")
    HttpPollingAdapter(transport=httpx.MockTransport(handler)).cancel(handle)
    assert budgets == [pytest.approx(10.0)]  # the read is bounded by the cap
    assert cancel_timeouts == [pytest.approx(2.0)]  # 10 s cap - 8 s read

    delay["sec"] = 12.0  # the read alone uses up the cap: no cancel request
    HttpPollingAdapter(transport=httpx.MockTransport(handler)).cancel(handle)
    assert len(cancel_timeouts) == 1

    delay["sec"] = 5.0
    fresh = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    started = clock.now
    fresh.restore(handle)
    assert budgets[-1] == pytest.approx(30.0)
    assert fresh.deadline_for(handle) == pytest.approx(started + 30.0)  # not +35


def _probe_registry_read(monkeypatch, read_setting) -> dict:
    seen: dict = {}
    real_get = registry.get_model

    def probe(mid, db=None):
        if db is not None:
            seen["inside"] = read_setting(db)
        return real_get(mid, db)

    monkeypatch.setattr(registry, "get_model", probe)
    return seen


def test_b9a_r2_m4_sqlite_registry_read_gets_a_busy_timeout(env, monkeypatch):
    """SQLite: the restore read runs with busy_timeout = the time left, and
    the pooled connection gets its own value back afterwards."""
    from tests.conftest import postgres_tests_enabled

    if postgres_tests_enabled():
        pytest.skip("SQLite variant (TEST_DATABASE_URL selects Postgres)")

    def busy(db):
        return db.connection().exec_driver_sql("PRAGMA busy_timeout").scalar()

    _register_model(monkeypatch, _moved_model("a"))
    with session_scope() as db:
        before = busy(db)
    seen = _probe_registry_read(monkeypatch, busy)
    assert registry.resolve_model("fake.moved-t2i", timeout=2.5)["model_id"] == "fake.moved-t2i"
    assert seen["inside"] == 2500
    with session_scope() as db:
        assert busy(db) == before
    with pytest.raises(StudioError) as ei:  # no time left: the DB is not touched
        registry.resolve_model("fake.moved-t2i", timeout=0.0)
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


def test_b9a_r2_m4_postgres_registry_read_gets_local_statement_timeouts(env, monkeypatch):
    """Postgres (TEST_DATABASE_URL only): the restore read runs with SET LOCAL
    statement_timeout / lock_timeout = the time left; nothing leaks into the
    next transaction on the pooled connection."""
    from sqlalchemy import text

    from tests.conftest import postgres_tests_enabled

    if not postgres_tests_enabled():
        pytest.skip("Postgres variant: set TEST_DATABASE_URL")

    def timeouts(db):
        return (
            db.execute(text("SHOW statement_timeout")).scalar(),
            db.execute(text("SHOW lock_timeout")).scalar(),
        )

    _register_model(monkeypatch, _moved_model("a"))
    with session_scope() as db:
        before = timeouts(db)
    seen = _probe_registry_read(monkeypatch, timeouts)
    assert registry.resolve_model("fake.moved-t2i", timeout=2.5)["model_id"] == "fake.moved-t2i"
    assert seen["inside"] == ("2500ms", "2500ms")
    with session_scope() as db:
        assert timeouts(db) == before


def test_b9a_r2_m5_cached_handle_still_checks_an_explicit_model_id(monkeypatch):
    """Review B9a-r2 M5: a handle the adapter already tracked returned before
    the explicit model-id check, so restore(handle, wrong entry) succeeded
    after a correct restore (or after submit). The check now runs first."""
    monkeypatch.setenv("FAKE_REF_KEY", "k")
    models = {m["model_id"]: m for m in (_endpoint_model("a"), _endpoint_model("b"))}
    monkeypatch.setattr(registry, "get_model", lambda mid, db=None: models[mid])

    def handler(req):
        if req.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "1"})
        return httpx.Response(200, json={"state": "QUEUED"})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    handle = http_polling.job_handle("1", "fake.a-t2i")
    adapter.restore(handle, models["fake.a-t2i"])
    deadline = adapter.deadline_for(handle)
    with pytest.raises(StudioError) as ei:
        adapter.restore(handle, models["fake.b-t2i"])
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    adapter.restore(handle, models["fake.a-t2i"])  # the right id: state kept
    assert adapter.deadline_for(handle) == deadline

    submitted = adapter.submit("fake.a-t2i", _PLAIN_PAYLOAD)  # cached by submit
    with pytest.raises(StudioError) as ei:
        adapter.restore(submitted, models["fake.b-t2i"])
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


def test_b9a_r2_m6_restore_failure_never_quotes_the_remote_id(monkeypatch):
    """Review B9a-r2 M6: the mismatch error quoted the handle before this
    model's secrets were collected, so a remote id reflecting a known env
    secret appeared in it. The message names model ids only."""
    secret = "m6-known-secret-" + "z9" * 20
    monkeypatch.setenv("FAKE_REF_KEY", secret)
    fresh = HttpPollingAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    def leaked(text: str) -> list[str]:
        return [secret[i:i + 24] for i in range(len(secret) - 23) if secret[i:i + 24] in text]

    handle = http_polling.job_handle(secret, "fake.plain-t2i")
    with pytest.raises(StudioError) as ei:
        fresh.restore(handle, _endpoint_model("b"))
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "fake.plain-t2i" in ei.value.message and "fake.b-t2i" in ei.value.message
    assert leaked(ei.value.message) == [], ei.value.message
    assert leaked(str(ei.value)) == []
    with pytest.raises(StudioError) as ei:  # not a handle at all: not quoted either
        fresh.restore(secret)
    assert leaked(ei.value.message) == [], ei.value.message


# ---------------------------------------------------------------------------
# B9a-r3 (Codex review of B9a-r2): M1 a failed restore never restarts the job
# budget, M2 the DB bound never loosens a limit already in force
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "read_sec,expected_budgets,elapsed",
    [
        (10.0, [10.0, 2.0], 12.0),  # the first read uses the whole budget
        (3.0, [10.0, 7.0, 2.0], 8.0),  # a quick failure: the retry gets only what is left
    ],
    ids=["read-exhausts-budget", "read-fails-early"],
)
def test_b9a_r3_m1_failed_restore_never_restarts_the_job_budget(
    monkeypatch, read_sec, expected_budgets, elapsed
):
    """Review B9R2-M1: when the restore inside deadline_for failed, the error
    was dropped, poll_until_done started a fresh local deadline and poll()'s
    re-restore got a fresh full budget again (budgets [10, 10, 2], 22 s for a
    10 s job). The deadline the first attempt started is kept; only cancel's
    own cap (2 s) comes on top, and it does not extend the job budget."""
    clock = _Clock()
    monkeypatch.setattr(http_polling, "_monotonic", clock.monotonic)
    monkeypatch.setattr(http_polling, "_sleep", clock.sleep)
    monkeypatch.setattr(defaults, "HTTP_JOB_TOTAL_DEADLINE_SEC", 10.0)
    monkeypatch.setattr(defaults, "HTTP_CANCEL_TIMEOUT_SEC", 2.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_MAX_SEC", 0.0)
    budgets: list[float] = []

    def failing_read(model_id, timeout=None):
        budgets.append(timeout)
        clock.now += min(read_sec, timeout)  # the read consumes its budget, then fails
        raise StudioError(ErrorCode.MODEL_UNAVAILABLE, "registry read failed")

    monkeypatch.setattr(registry, "resolve_model", failing_read)
    fresh = HttpPollingAdapter(
        transport=httpx.MockTransport(lambda r: pytest.fail(f"no request expected: {r.url}"))
    )
    handle = http_polling.job_handle("j1", "fake.plain-t2i")
    started = clock.now
    with pytest.raises(StudioError):
        poll_until_done(fresh, handle)
    assert budgets == pytest.approx(expected_budgets)
    assert clock.now - started == pytest.approx(elapsed)
    assert fresh.deadline_for(handle) == pytest.approx(started + 10.0)  # never restarted


def _sqlite_busy(db) -> int:
    return db.connection().exec_driver_sql("PRAGMA busy_timeout").scalar()


@pytest.mark.parametrize(
    "existing,budget,inside",
    [(5000, 60.0, 5000), (5000, 2.5, 2500), (0, 2.5, 0)],
    ids=["budget-larger-keeps-existing", "budget-smaller-applies", "zero-never-waits"],
)
def test_b9a_r3_m2_sqlite_bound_never_loosens_busy_timeout(env, existing, budget, inside):
    """Review B9R2-M2: the restore read set busy_timeout to the caller budget
    even when the connection already had a shorter one (5000 ms -> 6,000,000
    ms). The value is min(existing, budget); 0 (never wait) is kept."""
    from tests.conftest import postgres_tests_enabled

    if postgres_tests_enabled():
        pytest.skip("SQLite variant (TEST_DATABASE_URL selects Postgres)")
    with session_scope() as db:
        original = _sqlite_busy(db)
        db.connection().exec_driver_sql(f"PRAGMA busy_timeout = {existing}")
        try:
            with registry._bounded_session(db, budget):
                assert _sqlite_busy(db) == inside
            assert _sqlite_busy(db) == existing
        finally:
            db.connection().exec_driver_sql(f"PRAGMA busy_timeout = {original}")


def test_b9a_r3_m2_postgres_bound_never_loosens_a_finite_limit(env):
    """Postgres (TEST_DATABASE_URL only): a finite statement_timeout already in
    force is kept when the budget is larger; an unlimited one (0) gets the
    budget."""
    from sqlalchemy import text

    from tests.conftest import postgres_tests_enabled

    if not postgres_tests_enabled():
        pytest.skip("Postgres variant: set TEST_DATABASE_URL")
    with session_scope() as db:
        db.execute(text("SET LOCAL statement_timeout = 1000"))
        db.execute(text("SET LOCAL lock_timeout = 0"))
        with registry._bounded_session(db, 60.0):
            assert db.execute(text("SHOW statement_timeout")).scalar() == "1s"
            assert db.execute(text("SHOW lock_timeout")).scalar() == "1min"
