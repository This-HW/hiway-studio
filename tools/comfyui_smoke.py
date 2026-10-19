#!/usr/bin/env python3
"""Real-ComfyUI smoke test: one T2I frame through the full studio stack.

Unlike `make demo` (stub adapter, instant), this drives a REAL generation:
API -> eager Celery worker -> HttpPollingAdapter -> ComfyUI /prompt ->
/history poll -> /view download -> PNG on disk -> DB `succeeded`.

Prerequisites (manual):
  1. ComfyUI running with an SD1.5 checkpoint, e.g.
       ./venv/bin/python ComfyUI/main.py --cpu --port 8188
  2. COMFYUI_URL env pointing at it (the one-line machine move):
       COMFYUI_URL=http://127.0.0.1:8188/prompt

Usage:
  COMFYUI_URL=http://127.0.0.1:8188/prompt python3 tools/comfyui_smoke.py

Everything is self-contained under projects/comfyui-smoke-<timestamp>/.
On CPU-only machines a 512px/25-step SD1.5 frame takes a while; the
adapter polls up to ~90 minutes.
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
SMOKE_DIR = REPO_ROOT / "projects" / f"comfyui-smoke-{STAMP}"

COMFYUI_URL = os.environ.get("COMFYUI_URL", "").strip()
if not COMFYUI_URL:
    sys.exit("COMFYUI_URL is required, e.g. COMFYUI_URL=http://127.0.0.1:8188/prompt")

# --- environment must be fixed BEFORE any app.* import ---
os.environ["DATABASE_URL"] = f"sqlite:///{SMOKE_DIR}/smoke.db"
os.environ["PROJECTS_ROOT"] = str(SMOKE_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/0"  # unused: eager tasks + memory SSE
os.environ["SSE_BACKEND"] = "memory"

sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from app.core.sse_bridge import reset_bridge  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.models.system import Model  # noqa: E402
from app.models_lib.seed import apply_seed as apply_model_seed  # noqa: E402
from app.tasks import tasks_image  # noqa: E402,F401  (register worker)
from app.tasks.celery_app import celery_app  # noqa: E402

BASE = "/api/v1"
MODEL_ID = "comfyui.t2i-sd15"


def check(resp, expect: int, what: str):
    if resp.status_code != expect:
        raise RuntimeError(
            f"smoke failed at {what}: expected {expect}, got {resp.status_code}\n{resp.text[:800]}"
        )
    return resp.json() if resp.content else None


def main() -> int:
    t_all = time.time()
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"smoke dir: {SMOKE_DIR}", flush=True)
    print(f"COMFYUI_URL={COMFYUI_URL}", flush=True)

    print("\n==> initializing database + seeds", flush=True)
    Base.metadata.create_all(get_engine())
    n_models = apply_model_seed()
    # The ComfyUI seeds ship disabled (no ComfyUI in a fresh checkout);
    # flip to active for this smoke run (DB-only, seed file untouched).
    with session_scope() as db:
        row = db.get(Model, MODEL_ID)
        assert row is not None, f"seed model missing: {MODEL_ID}"
        row.status = "active"
    celery_app.conf.task_always_eager = True
    reset_bridge()
    client = TestClient(app.main.app)
    print(f"    ok ({n_models} models seeded, {MODEL_ID} activated)", flush=True)

    print("\n==> project + scene + shot", flush=True)
    body = check(
        client.post(f"{BASE}/projects", json={"title": f"ComfyUI smoke {STAMP}"}),
        201, "create project",
    )
    pid = body["id"]
    body = check(client.post(f"{BASE}/projects/{pid}/scenes", json={"title": "Smoke"}),
                 201, "create scene")
    sid = body["id"]
    body = check(
        client.post(f"{BASE}/scenes/{sid}/shots",
                    json={"action": "A lighthouse on a cliff at dawn, waves below",
                          "duration_sec": 3}),
        201, "create shot",
    )
    shot_id = body["id"]
    print(f"    ok (shot={shot_id})", flush=True)

    print("\n==> REAL T2I frame via ComfyUI (this takes a while on CPU) ...", flush=True)
    t0 = time.time()
    body = check(
        client.post(f"{BASE}/shots/{shot_id}/frame",
                    json={"model_id": MODEL_ID, "batch_size": 1, "seed": 42}),
        202, "generate frame",
    )
    job_id = body["job_id"]
    print(f"    job accepted: {job_id} (eager worker ran inline)", flush=True)
    job = check(client.get(f"{BASE}/jobs/{job_id}"), 200, "job detail")
    batch = job["batch"] if "batch" in job else job
    assert batch["status"] == "succeeded", f"job did not succeed: {batch['status']}"
    child = job["children"][0]
    assert child["status"] == "succeeded", f"child did not succeed: {child['status']}"
    rel = child["output_path"]
    assert rel, "child has no output_path"
    out = SMOKE_DIR / rel
    assert out.is_file(), f"output missing: {out}"
    el = time.time() - t0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    print(f"    ok ({el:.0f}s) -> {rel} [{probe.stdout.strip()}]", flush=True)
    print(f"\nSMOKE PASSED in {time.time() - t_all:.0f}s: real pixels from ComfyUI.", flush=True)
    print(f"  file: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
