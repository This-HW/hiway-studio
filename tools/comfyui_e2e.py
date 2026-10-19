#!/usr/bin/env python3
"""Real-ComfyUI E2E: project -> character master -> sheet -> 3 shots -> frames -> approvals.

Full studio image pipeline on REAL pixels (no stubs):
API -> eager Celery worker -> HttpPollingAdapter -> ComfyUI -> PNG -> DB.

Prerequisites: same as tools/comfyui_smoke.py (ComfyUI up, COMFYUI_URL set).
Run: COMFYUI_URL=http://127.0.0.1:8188/prompt python3 tools/comfyui_e2e.py

On CPU-only machines each 512px/25-step frame takes ~20 min; this E2E makes
5 images (master, sheet front view, 3 shot frames), so expect ~2 hours.
Everything is self-contained under projects/comfyui-e2e-<timestamp>/.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
E2E_DIR = REPO_ROOT / "projects" / f"comfyui-e2e-{STAMP}"

COMFYUI_URL = os.environ.get("COMFYUI_URL", "").strip()
if not COMFYUI_URL:
    sys.exit("COMFYUI_URL is required, e.g. COMFYUI_URL=http://127.0.0.1:8188/prompt")

os.environ["DATABASE_URL"] = f"sqlite:///{E2E_DIR}/e2e.db"
os.environ["PROJECTS_ROOT"] = str(E2E_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SSE_BACKEND"] = "memory"

sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from app.core.sse_bridge import reset_bridge  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.models.system import Model  # noqa: E402
from app.models_lib.seed import apply_seed as apply_model_seed  # noqa: E402
from app.tasks import tasks_image  # noqa: E402,F401
from app.tasks.celery_app import celery_app  # noqa: E402

BASE = "/api/v1"
MODEL_ID = "comfyui.t2i-sd15"
outputs: list[tuple[str, str]] = []


def check(resp, expect: int, what: str):
    if resp.status_code != expect:
        raise RuntimeError(
            f"e2e failed at {what}: expected {expect}, got {resp.status_code}\n{resp.text[:800]}"
        )
    return resp.json() if resp.content else None


def wait_job(client, job_id: str, what: str) -> dict:
    """With eager celery the worker already ran inline; just assert success."""
    job = check(client.get(f"{BASE}/jobs/{job_id}"), 200, f"job detail {what}")
    node = job.get("batch", job)
    assert node["status"] == "succeeded", f"{what} job {job_id}: {node['status']}"
    return node


def record(label: str, rel: str) -> None:
    assert rel, f"{label}: no output_path"
    out = E2E_DIR / rel
    assert out.is_file(), f"{label}: output missing: {out}"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    size = out.stat().st_size
    assert size > 10_000, f"{label}: suspiciously small file ({size} bytes)"
    outputs.append((label, f"{rel} [{probe.stdout.strip()}] {size // 1024}KB"))
    print(f"    ok {label} -> {rel} [{probe.stdout.strip()}]", flush=True)


def main() -> int:
    t_all = time.time()
    E2E_DIR.mkdir(parents=True, exist_ok=True)
    print(f"e2e dir: {E2E_DIR}", flush=True)

    print("\n==> init db + seeds", flush=True)
    Base.metadata.create_all(get_engine())
    n_models = apply_model_seed()
    with session_scope() as db:
        row = db.get(Model, MODEL_ID)
        assert row is not None, f"seed model missing: {MODEL_ID}"
        row.status = "active"
    celery_app.conf.task_always_eager = True
    reset_bridge()
    client = TestClient(app.main.app)
    print(f"    ok ({n_models} models, {MODEL_ID} active)", flush=True)

    print("\n==> project + character", flush=True)
    p = check(client.post(f"{BASE}/projects", json={"title": f"ComfyUI E2E {STAMP}"}),
              201, "create project")
    pid = p["id"]
    c = check(
        client.post(f"{BASE}/projects/{pid}/characters", json={
            "name": "Mira",
            "appearance_prompt": (
                "portrait of a young woman explorer, short black hair, "
                "green field jacket, warm cinematic light"
            ),
            "negative_prompt": "blurry, deformed",
            "description": "E2E test character",
        }),
        201, "create character",
    )
    cid = c["id"]
    print(f"    ok (character={cid})", flush=True)

    print("\n==> [1/5] character master portrait (REAL) ...", flush=True)
    t0 = time.time()
    r = check(client.post(f"{BASE}/characters/{cid}/master",
                          json={"model_id": MODEL_ID}), 202, "master")
    node = wait_job(client, r["job_id"], "master")
    record("master", node["output_path"])
    print(f"    ({time.time() - t0:.0f}s)", flush=True)

    print("\n==> [2/5] character sheet: front view (REAL) ...", flush=True)
    t0 = time.time()
    r = check(client.post(f"{BASE}/characters/{cid}/sheet",
                          json={"views": ["turnaround_front"]}), 202, "sheet")
    assert len(r["job_ids"]) == 1, f"expected 1 sheet job, got {len(r['job_ids'])}"
    node = wait_job(client, r["job_ids"][0], "sheet")
    record("sheet/front", node["output_path"])
    print(f"    ({time.time() - t0:.0f}s)", flush=True)

    print("\n==> scene + 3 shots", flush=True)
    s = check(client.post(f"{BASE}/projects/{pid}/scenes", json={"title": "E2E"}),
              201, "create scene")
    sid = s["id"]
    shot_ids = []
    actions = [
        "Mira standing on a cliff overlooking the sea at dawn",
        "close-up of Mira's face, wind in her hair, determined look",
        "Mira walking through a misty pine forest, backpack on",
    ]
    for i, action in enumerate(actions, 1):
        sh = check(client.post(f"{BASE}/scenes/{sid}/shots",
                               json={"action": action, "duration_sec": 3}),
                   201, f"create shot {i}")
        shot_ids.append(sh["id"])
    print(f"    ok ({len(shot_ids)} shots)", flush=True)

    for i, shot_id in enumerate(shot_ids, 1):
        print(f"\n==> [{2 + i}/5] shot {i} frame (REAL) ...", flush=True)
        t0 = time.time()
        r = check(client.post(f"{BASE}/shots/{shot_id}/frame",
                              json={"model_id": MODEL_ID, "batch_size": 1,
                                    "seed": 100 + i}),
                  202, f"frame shot {i}")
        job = check(client.get(f"{BASE}/jobs/{r['job_id']}"), 200, "job detail")
        batch = job.get("batch", job)
        assert batch["status"] == "succeeded", f"shot {i}: {batch['status']}"
        child = (job.get("children") or [batch])[0]
        assert child["status"] == "succeeded", f"shot {i} child: {child['status']}"
        record(f"shot{i}/frame", child["output_path"])
        ap = check(client.post(f"{BASE}/shots/{shot_id}/frame/approve",
                               json={"generation_id": child["id"]}),
                   200, f"approve shot {i}")
        assert ap.get("approved_frame_generation_id") == child["id"], \
            f"shot {i} approval did not stick: {ap.get('approved_frame_generation_id')}"
        print(f"    approved ({time.time() - t0:.0f}s)", flush=True)

    print("\n" + "=" * 60)
    print(f"E2E PASSED in {(time.time() - t_all) / 60:.0f} min — real pixels end to end:")
    for label, desc in outputs:
        print(f"  - {label}: {desc}")
    print(f"  dir: {E2E_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
