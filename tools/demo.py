#!/usr/bin/env python3
"""`make demo` — the full P0 pipeline on the stub adapter. No API keys needed.

Pipeline:
  project -> character (master portrait + sheet) -> scene + 3 shots
  -> frames + approval -> 3 I2V clips -> timeline -> subtitles (2-3 cues)
  -> QC -> one rendered mp4.

Everything is self-contained under projects/demo-<timestamp>/: the SQLite
database (demo.db), all media, and the final output.mp4. The directory is
git-ignored, so it is safe to re-run; each run creates a fresh directory.

Runs fully in-process (TestClient + eager Celery + SQLite + in-memory SSE),
so it needs no docker, redis, postgres, or network access. Target runtime:
well under 5 minutes (stub adapters respond instantly; only ffmpeg work
takes real time).

Usage:
  make demo
  # or directly:
  python3 tools/demo.py [--title "..."]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
DEMO_DIR = REPO_ROOT / "projects" / f"demo-{STAMP}"

# --- environment must be fixed BEFORE any app.* import (config has no defaults)
os.environ["DATABASE_URL"] = f"sqlite:///{DEMO_DIR / 'demo.db'}"
os.environ["PROJECTS_ROOT"] = str(DEMO_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/0"  # unused: eager tasks + memory SSE
os.environ["SSE_BACKEND"] = "memory"

sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from app.core.sse_bridge import reset_bridge  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.models_lib.seed import apply_seed as apply_model_seed  # noqa: E402
from app.services.exports import ensure_builtin_presets  # noqa: E402
from app.services.style_kit import seed_category_packs  # noqa: E402
from app.tasks import tasks_audio, tasks_image, tasks_post, tasks_video  # noqa: E402,F401
from app.tasks.celery_app import celery_app  # noqa: E402

BASE = "/api/v1"
SHOTS = [
    {
        "action": "A lighthouse stands on a cliff at dawn. Waves crash below.",
        "dialogue": "새벽이 온다. 등대가 불을 밝힌다.",
        "motion": "camera slowly pushes in toward the lighthouse",
    },
    {
        "action": "A small boat approaches the rocky shore through morning mist.",
        "dialogue": "안개 사이로 작은 배가 다가온다.",
        "motion": "boat drifts left to right through the mist",
    },
    {
        "action": "A seagull lands on the lighthouse railing as the sun rises.",
        "dialogue": "갈매기가 난간에 앉는다. 해가 뜬다.",
        "motion": "seagull lands, wings folding",
    },
]


def step(name: str) -> float:
    print(f"\n==> {name}", flush=True)
    return time.time()


def done(t0: float, extra: str = "") -> None:
    print(f"    ok ({time.time() - t0:.1f}s){' — ' + extra if extra else ''}", flush=True)


def check(resp, expect: int, what: str):
    if resp.status_code != expect:
        raise RuntimeError(
            f"demo failed at {what}: expected {expect}, got {resp.status_code}\n{resp.text[:800]}"
        )
    return resp.json() if resp.content else None


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the stub-adapter P0 demo.")
    ap.add_argument("--title", default="Demo — Lighthouse", help="Demo project title.")
    args = ap.parse_args()

    t_all = time.time()
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    print(f"demo dir: {DEMO_DIR}")

    t0 = step("initializing database + seeds")
    Base.metadata.create_all(get_engine())
    n_models = apply_model_seed()
    seed_dir = REPO_ROOT / "infra" / "seeds" / "category_packs"
    with session_scope() as db:
        n_packs = seed_category_packs(db, seed_dir)
        n_presets = ensure_builtin_presets(db)
    celery_app.conf.task_always_eager = True
    reset_bridge()
    client = TestClient(app.main.app)
    done(t0, f"{n_models} models, {n_packs} category packs, {n_presets} presets")

    # --- project ---------------------------------------------------------
    t0 = step("1. project")
    body = check(
        client.post(f"{BASE}/projects",
                    json={"title": args.title, "category_pack_code": "knowledge_info"}),
        201, "create project",
    )
    pid = body["id"]
    done(t0, f"id={pid}")

    # --- character: master portrait + sheet ------------------------------
    t0 = step("2. character (master portrait + sheet)")
    body = check(
        client.post(f"{BASE}/projects/{pid}/characters",
                    json={"name": "Mira the Keeper",
                          "appearance_prompt": "a lighthouse keeper woman in her 40s, "
                                               "weathered coat, warm smile"}),
        201, "create character",
    )
    cid = body["id"]
    check(client.post(f"{BASE}/characters/{cid}/master", json={}), 202, "master portrait")
    refs = check(client.get(f"{BASE}/characters/{cid}/references"), 200, "list references")
    masters = [r for r in refs if r["role"] == "master" and r["is_primary"]]
    assert masters, "no primary master portrait"
    master_rel = masters[0]["image_path"]
    assert (DEMO_DIR / master_rel).is_file(), master_rel
    body = check(
        client.post(f"{BASE}/characters/{cid}/sheet",
                    json={"views": ["turnaround_front"], "expressions": ["happy"]}),
        202, "character sheet",
    )
    assert len(body["job_ids"]) == 2
    done(t0, f"master={master_rel}, sheet jobs={len(body['job_ids'])}")

    # --- scene + 3 shots ---------------------------------------------------
    t0 = step("3. scene + 3 shots")
    body = check(client.post(f"{BASE}/projects/{pid}/scenes", json={"title": "Dawn"}),
                 201, "create scene")
    sid = body["id"]
    shot_ids: list[str] = []
    for s in SHOTS:
        body = check(client.post(f"{BASE}/scenes/{sid}/shots",
                                 json={"action": s["action"], "duration_sec": 3}),
                     201, "create shot")
        shot_id = body["id"]
        shot_ids.append(shot_id)
        check(client.patch(f"{BASE}/shots/{shot_id}", json={"dialogue": s["dialogue"]}),
              200, "set dialogue")
    done(t0, f"{len(shot_ids)} shots")

    # --- frames + approval -------------------------------------------------
    t0 = step("4. frames + approval (3 shots)")
    for shot_id in shot_ids:
        body = check(client.post(f"{BASE}/shots/{shot_id}/frame", json={"batch_size": 1}),
                     202, "generate frame")
        job = check(client.get(f"{BASE}/jobs/{body['job_id']}"), 200, "job detail")
        take = job["children"][0]["id"]
        body = check(client.post(f"{BASE}/shots/{shot_id}/frame/approve",
                                 json={"generation_id": take}),
                     200, "approve frame")
        assert body["status"] == "frame_approved", body
    done(t0, "all approved")

    # --- I2V clips ----------------------------------------------------------
    t0 = step("5. I2V clips (3)")
    for shot_id, s in zip(shot_ids, SHOTS):
        check(client.post(f"{BASE}/shots/{shot_id}/video",
                          json={"mode": "i2v", "motion_prompt": s["motion"]}),
              202, "i2v clip")
        status = check(client.get(f"{BASE}/shots/{shot_id}"), 200, "shot status")["status"]
        assert status == "video_done", status
    done(t0, "3 clips done")

    # --- timeline ------------------------------------------------------------
    t0 = step("6. timeline assembly")
    body = check(client.post(f"{BASE}/projects/{pid}/timelines", json={"name": "Demo Cut"}),
                 201, "create timeline")
    tid = body["id"]
    body = check(client.post(f"{BASE}/timelines/{tid}/build-from-shots"), 200,
                 "build-from-shots")
    assert len(body["clip_order"]) == 3, body["clip_order"]
    done(t0, f"{len(body['clip_order'])} clips in order")

    # --- subtitles ------------------------------------------------------------
    t0 = step("7. subtitles (auto-generated from dialogue)")
    body = check(client.post(f"{BASE}/projects/{pid}/subtitle-tracks",
                             json={"language_code": "ko", "label": "한국어"}),
                 201, "create subtitle track")
    track_id = body["id"]
    cues = check(client.post(f"{BASE}/subtitle-tracks/{track_id}/generate"), 201,
                 "generate cues")
    assert len(cues) >= 2, cues
    check(client.put(f"{BASE}/projects/{pid}/subtitle-style",
                     json={"font_size_px": 28, "position": "bottom"}),
          200, "subtitle style")
    check(client.patch(f"{BASE}/timelines/{tid}",
                       json={"subtitle_mode": "single",
                             "subtitle_track_ids": [track_id]}),
          200, "attach subtitles to timeline")
    done(t0, f"{len(cues)} cues")

    # --- export preset (small, with burn-in) -----------------------------------
    t0 = step("8. export preset (Demo 640x360, burn-in)")
    body = check(client.post(f"{BASE}/projects/{pid}/export-presets",
                             json={"name": "Demo 640x360", "aspect_ratio": "16:9",
                                   "resolution": "640x360", "fps": 24,
                                   "burn_subtitles": True}),
                 200, "create preset")
    preset_id = body["id"]
    done(t0)

    # --- QC ---------------------------------------------------------------------
    t0 = step("9. QC gate")
    body = check(client.post(f"{BASE}/timelines/{tid}/qc"), 202, "run QC")
    report = check(client.get(f"{BASE}/qc-reports/{body['qc_report_id']}"), 200, "qc report")
    overall = report["overall"]
    n_checks = len(report["checks"])
    failed = [c["code"] for c in report["checks"] if c["status"] == "fail"]
    if overall == "fail":
        raise RuntimeError(f"QC failed: {failed}")
    ignore = overall == "warn"
    done(t0, f"overall={overall}, {n_checks} checks")

    # --- render ------------------------------------------------------------------
    t0 = step(f"10. render (ignore_warnings={ignore})")
    check(client.post(f"{BASE}/timelines/{tid}/render",
                      json={"export_preset_id": preset_id, "ignore_warnings": ignore}),
          202, "render")
    body = check(client.get(f"{BASE}/timelines/{tid}"), 200, "timeline status")
    assert body["status"] == "done", body
    out_rel = body["output_path"]
    out_abs = DEMO_DIR / out_rel
    assert out_abs.is_file(), out_rel
    facts = ffprobe(out_abs)
    vs = next(s for s in facts["streams"] if s["codec_type"] == "video")
    dur = float(facts["format"]["duration"])
    done(t0, f"{out_rel} ({vs['width']}x{vs['height']} {vs['codec_name']}, {dur:.1f}s)")

    # --- costs --------------------------------------------------------------------
    summary = check(client.get(f"{BASE}/projects/{pid}/costs/summary"), 200, "cost summary")
    total = summary.get("total_actual_usd", 0)

    elapsed = time.time() - t_all
    print("\n" + "=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)
    print(f"  project dir : {DEMO_DIR}")
    print(f"  output mp4  : {out_abs}")
    print(f"  duration    : {dur:.1f}s, {vs['width']}x{vs['height']}, {vs['codec_name']}")
    print(f"  subtitles   : {len(cues)} cues burned in (ko)")
    print(f"  QC          : {overall} ({n_checks} checks)")
    print(f"  cost (stub) : ${total}")
    print(f"  elapsed     : {elapsed:.1f}s")
    print()
    print("  view the result:")
    print(f"    ffplay {out_abs}")
    print("  (projects/ is git-ignored; delete the demo dir any time.)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        print(f"\nDEMO FAILED: {e}", file=sys.stderr)
        raise SystemExit(1)
