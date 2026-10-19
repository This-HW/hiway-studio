#!/usr/bin/env python3
"""Seedance 2.5 (via Higgsfield) pilot: 3 x 5s clips for billing + quality check.

Real-money pilot. Prerequisites (HW):
  1. Higgsfield account + small top-up
  2. API key issued
Run: HF_API_KEY=... python3 tools/higgsfield_pilot.py

What it does:
  API -> eager Celery worker -> HttpPollingAdapter -> api.higgsfield.ai
  -> MP4 -> DB. 3 clips x 5s x $0.144/s = ~$2.16 expected.

Everything is self-contained under projects/higgsfield-pilot-<timestamp>/.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
PILOT_DIR = REPO_ROOT / "projects" / f"higgsfield-pilot-{STAMP}"

HF_API_KEY = os.environ.get("HF_API_KEY", "").strip()
if not HF_API_KEY:
    sys.exit("HF_API_KEY is required, e.g. HF_API_KEY=... python3 tools/higgsfield_pilot.py")

os.environ["DATABASE_URL"] = "sqlite:///" + str(PILOT_DIR / "pilot.db")
os.environ["PROJECTS_ROOT"] = str(PILOT_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SSE_BACKEND"] = "memory"

sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from app.core.sse_bridge import reset_bridge  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.models_lib.seed import apply_seed as apply_model_seed  # noqa: E402
from app.tasks import tasks_video  # noqa: E402,F401
from app.tasks.celery_app import celery_app  # noqa: E402

BASE = "/api/v1"
MODEL_ID = "higgsfield.seedance-25-t2v"
PRICE_PER_SEC = 0.144
DURATION_SEC = 5

# 3 distinct prompts: indoor dialogue-ish, outdoor motion, night neon.
PROMPTS = [
    "A cozy Korean cafe interior at dusk, warm tungsten light, barista pouring latte art, slow dolly-in, cinematic",
    "Cherry blossoms falling over a quiet Seoul street in spring morning, gentle breeze, tracking shot, photorealistic",
    "Neon-lit alley in the rain at night, reflections on wet asphalt, a figure with umbrella walking away, moody",
]

PILOT_DIR.mkdir(parents=True, exist_ok=True)
reset_bridge()
celery_app.conf.task_always_eager = True

engine = get_engine()
Base.metadata.create_all(engine)
apply_model_seed(engine, REPO_ROOT / "infra" / "seeds" / "models" / "higgsfield.json")

client = TestClient(app.main.app)

with session_scope() as db:
    from app.models.system import Model

    model = db.query(Model).filter_by(model_id=MODEL_ID).one()
    model.status = "active"
    db.add(model)

proj = client.post(f"{BASE}/projects", json={"name": f"higgsfield-pilot-{STAMP}"}).json()
project_id = proj["id"]
scene = client.post(
    f"{BASE}/projects/{project_id}/scenes", json={"title": "pilot", "order": 1}
).json()
shot = client.post(
    f"{BASE}/projects/{project_id}/scenes/{scene['id']}/shots",
    json={"order": 1, "prompt": PROMPTS[0]},
).json()
shot_id = shot["id"]

results = []
for i, prompt in enumerate(PROMPTS, 1):
    print(f"[{i}/3] submitting clip...", flush=True)
    resp = client.post(
        f"{BASE}/shots/{shot_id}/video",
        json={
            "mode": "t2v",
            "motion_prompt": prompt,
            "duration_sec": DURATION_SEC,
            "model_id": MODEL_ID,
        },
        headers={"Idempotency-Key": f"pilot-{STAMP}-{i}"},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    deadline = time.time() + 1200  # 20 min per clip max
    status, video_path = "?", None
    while time.time() < deadline:
        time.sleep(15)
        jobs = client.get(f"{BASE}/jobs").json()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            status = "job vanished"
            break
        status = job["status"]
        if status in ("succeeded", "failed"):
            videos = client.get(f"{BASE}/shots/{shot_id}/videos").json()
            video_path = (videos or [{}])[0].get("file_path")
            break
        print(f"[{i}/3] status={status} ...", flush=True)

    cost = PRICE_PER_SEC * DURATION_SEC
    results.append(
        {"prompt": prompt[:40], "status": status, "path": video_path, "cost_usd": cost}
    )
    print(f"[{i}/3] done: status={status} path={video_path}", flush=True)

print("\n=== PILOT RESULT ===")
total = 0.0
for r in results:
    total += r["cost_usd"]
    print(f"- [{r['status']}] ${r['cost_usd']:.3f} {r['prompt']}... -> {r['path']}")
print(f"total expected billing: ~${total:.2f} (3 clips x {DURATION_SEC}s x ${PRICE_PER_SEC}/s)")
print(f"files under: {PILOT_DIR}")
