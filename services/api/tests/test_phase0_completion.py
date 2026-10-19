"""Phase-0 completion proof.

Proves, with the stub adapter and no API keys:
  1. a text_to_image job can be put on the queue (q.image.run_generation),
  2. progress arrives over SSE (job.progress events, then job.completed),
  3. the artifact is saved under projects/{uuid}/ with the naming rule,
  4. the DB stores a relative path only,
  5. succeeded artifacts cannot be overwritten (immutability guard),
  6. GET /api/v1/events streams real SSE to an HTTP client.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.core.sse_bridge import get_bridge, reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.files import immutability
from app.models.generation import Generation
from app.tasks import tasks_image
from app.tasks.celery_app import celery_app

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "packages" / "contracts"


@pytest.fixture()
def phase0_env(tmp_path, monkeypatch):
    """Isolated env: fresh PROJECTS_ROOT, in-memory DB, memory SSE bus."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()

    test_engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(sess_mod, "get_engine", lambda: test_engine)
    reset_engine()
    # Rebuild the session factory against the test engine.
    sess_mod._SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    Base.metadata.create_all(test_engine)

    celery_app.conf.task_always_eager = True
    yield tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def test_stub_text_to_image_end_to_end(phase0_env):
    project_id = uuid.uuid4()
    generation_id = uuid.uuid4()

    with session_scope() as db:
        db.add(
            Generation(
                id=generation_id,
                project_id=project_id,
                job_type="text_to_image",
                status="queued",
                model_id="stub.t2i-fast",
                params={"prompt": "a red bicycle on a beach"},
            )
        )

    seen: list[dict] = []

    async def collect():
        async for msg in get_bridge().subscribe():
            if msg.get("heartbeat"):
                continue
            if msg.get("event") in ("job.progress", "job.completed", "job.failed"):
                seen.append(msg)
            if msg.get("event") in ("job.completed", "job.failed"):
                break

    async def main():
        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # let the subscription register first
        result = await asyncio.to_thread(
            lambda: tasks_image.run_generation.delay(str(generation_id)).get()
        )
        await asyncio.wait_for(collector, timeout=15)
        return result

    summary = asyncio.run(main())

    # 1+2: queue ran the job; SSE carried progress then completion.
    assert summary["status"] == "succeeded"
    kinds = [m["event"] for m in seen]
    assert "job.progress" in kinds, f"no progress events: {kinds}"
    assert kinds[-1] == "job.completed"
    pcts = [m["data"]["progress_pct"] for m in seen if m["event"] == "job.progress"]
    assert len(pcts) >= 2 and pcts == sorted(pcts), pcts
    assert all(m["data"]["job_id"] == str(generation_id) for m in seen)

    # 3+4: file on disk under projects/{uuid}/, relative path in DB.
    with session_scope() as db:
        gen = db.get(Generation, generation_id)
        assert gen is not None
        assert gen.status == "succeeded"
        rel = gen.output_path
    assert rel is not None
    assert not os.path.isabs(rel), f"absolute path stored in DB: {rel}"
    assert rel == f"projects/{project_id}/frames/{generation_id}_0.png", rel
    abs_path = phase0_env / rel
    assert abs_path.is_file() and abs_path.stat().st_size > 0
    assert abs_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes

    # 5: immutability — overwriting the succeeded artifact must fail.
    with pytest.raises(immutability.ImmutableViolation):
        immutability.write_new(abs_path, b"overwrite attempt")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_events_endpoint_streams_sse(phase0_env):
    """End to end over real HTTP: uvicorn serves the app, an HTTP client
    receives SSE frames. (httpx ASGITransport cannot stream infinite bodies,
    so a real server socket is used.)"""
    import threading

    import httpx
    import uvicorn

    from app.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        received: list[str] = []
        # trust_env=False: the sandbox sets proxy env vars that must not apply to localhost.
        with httpx.Client(trust_env=False) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/api/v1/events", timeout=10) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")

                def _publish():
                    time.sleep(0.5)  # let the SSE subscription register
                    get_bridge().publish("job_events", "job.progress", {"job_id": "probe"})

                publisher = threading.Thread(target=_publish, daemon=True)
                publisher.start()
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        received.append(line)
                    elif line.startswith("data:"):
                        received.append(line)
                        break
                publisher.join(timeout=5)

        assert received[0] == "event: job.progress", received
        assert received[1].startswith('data: {"job_id": "probe"}'), received
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_error_contracts_mirror():
    """packages/contracts/errors.json must mirror core/errors.py (14 codes)."""
    path = CONTRACTS_DIR / "errors.json"
    assert path.is_file(), f"contracts not generated yet: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    codes = [e["code"] for e in data["errors"]]
    assert len(codes) == len(ErrorCode.ALL), codes
    assert set(codes) == set(ErrorCode.ALL), set(codes) ^ set(ErrorCode.ALL)


def test_healthz():
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
