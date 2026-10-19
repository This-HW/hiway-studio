#!/usr/bin/env python3
"""Resume the real-ComfyUI E2E from an existing run dir after an interruption.

Reuses the interrupted run's DB: master/sheet images already on disk are kept,
orphaned queued generations are cancelled, and only shots without an approved
frame get a new real frame generation + approval.

Usage:
  COMFYUI_URL=http://127.0.0.1:8188/prompt python3 tools/comfyui_e2e_resume.py \
      /home/hatch/workspace/ai-studio/projects/comfyui-e2e-<stamp>
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("usage: comfyui_e2e_resume.py <e2e-run-dir>")
E2E_DIR = Path(sys.argv[1]).resolve()
assert (E2E_DIR / "e2e.db").is_file(), f"no e2e.db in {E2E_DIR}"

REPO_ROOT = Path(__file__).resolve().parents[1]
COMFYUI_URL = os.environ.get("COMFYUI_URL", "").strip()
if not COMFYUI_URL:
    sys.exit("COMFYUI_URL is required")

os.environ["DATABASE_URL"] = f"sqlite:///{E2E_DIR}/e2e.db"
os.environ["PROJECTS_ROOT"] = str(E2E_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SSE_BACKEND"] = "memory"

sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from app.core.sse_bridge import reset_bridge  # noqa: E402
from app.tasks import tasks_image  # noqa: E402,F401
from app.tasks.celery_app import celery_app  # noqa: E402

BASE = "/api/v1"
MODEL_ID = "comfyui.t2i-sd15"


def check(resp, expect: int, what: str):
    if resp.status_code != expect:
        raise RuntimeError(
            f"resume failed at {what}: expected {expect}, got {resp.status_code}\n{resp.text[:500]}"
        )
    return resp.json() if resp.content else None


def main() -> int:
    t_all = time.time()
    print(f"resuming e2e dir: {E2E_DIR}", flush=True)
    celery_app.conf.task_always_eager = True
    reset_bridge()
    client = TestClient(app.main.app)

    # Cancel orphaned queued generations from the killed run (tolerate 400s:
    # cancelling a batch parent cascades to its children).
    jobs = check(client.get(f"{BASE}/jobs?status=queued"), 200, "list queued")
    for j in jobs:
        r = client.post(f"{BASE}/jobs/{j['id']}/cancel")
        print(f"  cancel orphan {j['id'][:8]}: {r.status_code}", flush=True)

    projects = check(client.get(f"{BASE}/projects"), 200, "list projects")
    assert projects, "no project in resumed db"
    pid = projects[0]["id"]
    scenes = check(client.get(f"{BASE}/projects/{pid}/scenes"), 200, "list scenes")
    sid = scenes[0]["id"]
    shots = check(client.get(f"{BASE}/scenes/{sid}/shots"), 200, "list shots")
    print(f"  {len(shots)} shots to check", flush=True)

    done = 0
    for i, sh in enumerate(shots, 1):
        if sh.get("approved_frame_generation_id"):
            print(f"  shot {i}: already approved, skipping", flush=True)
            done += 1
            continue
        print(f"\n==> shot {i} frame (REAL) ...", flush=True)
        t0 = time.time()
        r = check(client.post(f"{BASE}/shots/{sh['id']}/frame",
                              json={"model_id": MODEL_ID, "batch_size": 1,
                                    "seed": 100 + i}),
                  202, f"frame shot {i}")
        job = check(client.get(f"{BASE}/jobs/{r['job_id']}"), 200, "job detail")
        batch = job.get("batch", job)
        assert batch["status"] == "succeeded", f"shot {i}: {batch['status']}"
        child = (job.get("children") or [batch])[0]
        assert child["status"] == "succeeded", f"shot {i} child: {child['status']}"
        out = E2E_DIR / child["output_path"]
        assert out.is_file(), f"shot {i}: missing {out}"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True, timeout=60)
        ap = check(client.post(f"{BASE}/shots/{sh['id']}/frame/approve",
                               json={"generation_id": child["id"]}),
                   200, f"approve shot {i}")
        assert ap.get("approved_frame_generation_id") == child["id"]
        print(f"    ok shot {i} [{probe.stdout.strip()}] approved "
              f"({time.time() - t0:.0f}s)", flush=True)
        done += 1

    print(f"\nRESUME DONE in {(time.time() - t_all) / 60:.0f} min: "
          f"{done}/{len(shots)} shots approved.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
