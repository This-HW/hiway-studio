"""Job execution lifecycle (B4a, ADR-0013, plan D4/D7, G8 F02/F03/F05/P24/H2/P20/H1/RH1).

Proves:
  * failure bookkeeping survives the worker transaction (a NEW session sees
    failed / error_code / attempt rows / retry_count);
  * retry dispatches the task that owns the job_type; cancel cascades to
    Video/Shot and invalidates the running execution;
  * execution ownership: claim is exclusive, a stale execution (ABA) cannot
    write, redelivery of a terminal job never calls the provider;
  * dispatch_after_commit runs every callback even if one raises;
  * reconcile requeues/fails expired executions and re-dispatches stale
    queued rows; uncertain submits (D7) are never auto-resubmitted.

Runs on a per-test SQLite file by default; when TEST_DATABASE_URL points at
a dedicated Postgres test database (name ending in "test") the same tests run
there (schema reset per test, guarded by conftest.reset_test_postgres). The
service DATABASE_URL is never used (R2-C1).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

import app.core.celery_app as celery_mod
import app.db.session as sess_mod
import app.main  # noqa: F401  (imports every model module for create_all)
from app.core import defaults
from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.audio import AudioGeneration
from app.models.generation import Generation, GenerationAttempt, Video
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.system import Model
from app.models_lib import registry
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter
from app.tasks import tasks_audio, tasks_image, tasks_p1, tasks_video  # noqa: F401
from tests.conftest import isolated_engine, postgres_tests_enabled

TASK_NAMES = (
    "q.image.run_generation",
    "q.video.run_generation",
    "q.audio.run_generation",
    "q.p1.run_lora_train",
    "q.p1.run_style_extract",
    "q.p1.run_multishot",
)

def _real_png() -> bytes:
    # A decodable image: the worker verifies results with Pillow before
    # publishing (B7-r2 M3); a bare PNG signature is not an image.
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


_PNG = _real_png()


class FakeAdapter(ProviderAdapter):
    provider_id = "fake"

    def __init__(self) -> None:
        self.submits = 0
        self.cancels: list[str] = []
        self.final_state = "succeeded"
        self.final_error_code: str | None = None
        self.submit_delay = 0.0
        self.on_poll = None
        self._lock = threading.Lock()

    def submit(self, model_id: str, payload: dict) -> str:
        with self._lock:
            self.submits += 1
            n = self.submits
        if self.submit_delay:
            time.sleep(self.submit_delay)
        return f"fake-{n}"

    def poll(self, provider_job_id: str) -> JobStatus:
        if self.on_poll is not None:
            return self.on_poll(provider_job_id)
        return JobStatus(
            state=self.final_state,
            progress_pct=100.0,
            error_code=self.final_error_code,
            error_message="provider said no" if self.final_error_code else None,
        )

    def cancel(self, provider_job_id: str) -> None:
        self.cancels.append(provider_job_id)

    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        return EstimatedCost(Decimal("0"))

    def fetch_result(self, provider_job_id: str) -> bytes:
        return _PNG


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Temp SQLite, or the verified dedicated TEST_DATABASE_URL (R2-C1): the
    # service DATABASE_URL is never reset.
    url, engine = isolated_engine(tmp_path, "jobs.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    fake = FakeAdapter()
    monkeypatch.setitem(registry._ADAPTERS, "fake", fake)
    sent: list[tuple[str, tuple]] = []
    for name in TASK_NAMES:
        monkeypatch.setitem(
            celery_mod._JOB_DISPATCHERS,
            name,
            lambda *a, _n=name: sent.append((_n, a)),
        )
    monkeypatch.setattr(defaults, "RETRY_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(defaults, "RETRY_JITTER_SEC", 0.0)
    monkeypatch.setattr(defaults, "POLL_INTERVAL_START_SEC", 0.0)

    with session_scope() as db:
        db.add(
            Model(
                model_id="fake.model",
                modality="image",
                provider_id="fake",
                capabilities={},
                constraints={},
            )
        )
        db.add(
            Model(
                model_id="fake.video",
                modality="video",
                provider_id="fake",
                capabilities={},
                constraints={},
            )
        )
        project = Project(title="jobs")
        db.add(project)
        db.flush()
        pid = project.id
    yield {"fake": fake, "sent": sent, "project_id": pid, "tmp": tmp_path}
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()
    engine.dispose()


# --- helpers -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _image_job(pid, *, status="queued", **kw) -> str:
    with session_scope() as db:
        gen = Generation(
            project_id=pid,
            job_type="text_to_image",
            model_id="fake.model",
            prompt_final="a cat",
            status=status,
            **kw,
        )
        db.add(gen)
        db.flush()
        return str(gen.id)


def _shot(pid, *, status="draft", approved=False) -> uuid.UUID:
    with session_scope() as db:
        scene = Scene(project_id=pid, order_index=0)
        db.add(scene)
        db.flush()
        shot = Shot(scene_id=scene.id, order_index=0, status=status)
        db.add(shot)
        db.flush()
        if approved:
            frame = Generation(
                project_id=pid,
                shot_id=shot.id,
                job_type="text_to_image",
                model_id="fake.model",
                status="succeeded",
                output_path=f"projects/{pid}/frames/x.png",
            )
            db.add(frame)
            db.flush()
            shot.approved_frame_generation_id = frame.id
        return shot.id


def _video_job(
    pid, shot_id, *, status="queued", video_status="generating"
) -> tuple[str, str]:
    with session_scope() as db:
        gen = Generation(
            project_id=pid,
            shot_id=shot_id,
            job_type="image_to_video",
            model_id="fake.video",
            status=status,
            params={"mode": "i2v", "motion_prompt": "pan", "duration_sec": 5.0},
            error_code="PROVIDER_ERROR" if status == "failed" else None,
        )
        db.add(gen)
        db.flush()
        video = Video(
            shot_id=shot_id,
            generation_id=gen.id,
            mode="i2v",
            motion_prompt="pan",
            duration_sec=5.0,
            fps=24,
            resolution="1920x1080",
            model_id="fake.video",
            output_path="",
            status=video_status,
        )
        db.add(video)
        db.flush()
        return str(gen.id), str(video.id)


def _get(model, row_id):
    with session_scope() as db:
        row = db.get(model, uuid.UUID(str(row_id)))
        db.expunge_all()
        return row


def _attempts(gid) -> list[GenerationAttempt]:
    with session_scope() as db:
        rows = (
            db.query(GenerationAttempt)
            .filter(GenerationAttempt.generation_id == uuid.UUID(gid))
            .order_by(GenerationAttempt.attempt_no)
            .all()
        )
        db.expunge_all()
        return rows


def _run_image(gid):
    return tasks_image.run_generation.apply(args=[gid])


# --- F02/F03: failure persistence ---------------------------------------------


def test_terminal_failure_persists_in_new_session(env):
    env["fake"].final_state = "failed"
    env["fake"].final_error_code = ErrorCode.MODERATION_BLOCKED
    gid = _image_job(env["project_id"])

    _run_image(gid)

    gen = _get(Generation, gid)
    assert gen.status == "failed"
    assert gen.error_code == ErrorCode.MODERATION_BLOCKED
    assert gen.finished_at is not None
    attempts = _attempts(gid)
    assert len(attempts) == 1
    assert attempts[0].error_code == ErrorCode.MODERATION_BLOCKED
    assert attempts[0].finished_at is not None
    # The provider reported a definitive failure after submit: no automatic
    # resubmission (D7: only submit_state='none' may be auto-submitted).
    assert env["fake"].submits == 1


def test_retryable_failure_persists_retry_count(env):
    # Disabled explicit model -> MODEL_UNAVAILABLE (retryable) BEFORE submit.
    with session_scope() as db:
        db.get(Model, "fake.model").status = "disabled"
    gid = _image_job(env["project_id"])

    _run_image(gid)

    gen = _get(Generation, gid)
    assert gen.status == "failed"
    assert gen.error_code == ErrorCode.MODEL_UNAVAILABLE
    assert gen.retry_count == defaults.RETRY_MAX_ATTEMPTS
    assert len(_attempts(gid)) == defaults.RETRY_MAX_ATTEMPTS + 1
    assert all(a.error_code == ErrorCode.MODEL_UNAVAILABLE for a in _attempts(gid))
    assert env["fake"].submits == 0


# --- F05: retry per job_type ---------------------------------------------------


def test_retry_failed_image_to_video_dispatches_video_task(env):
    from app.services import generations as gen_service

    pid = env["project_id"]
    shot_id = _shot(pid, status="failed", approved=True)
    gid, vid = _video_job(pid, shot_id, status="failed", video_status="failed")

    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    assert env["sent"] == [("q.video.run_generation", (gid,))]
    gen = _get(Generation, gid)
    assert gen.status == "queued" and gen.error_code is None
    assert _get(Video, vid).status == "generating"
    assert _get(Shot, shot_id).status == "video_generating"


def test_retry_failed_tts_dispatches_audio_task(env):
    from app.services import generations as gen_service

    with session_scope() as db:
        row = AudioGeneration(
            project_id=env["project_id"],
            kind="dialogue",
            text="hi",
            status="failed",
            error_code="PROVIDER_ERROR",
        )
        db.add(row)
        db.flush()
        aid = str(row.id)
    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(aid))
    assert env["sent"] == [("q.audio.run_generation", (aid,))]
    assert _get(AudioGeneration, aid).status == "queued"


def test_retry_failed_lora_train_dispatches_p1_task(env):
    from app.models.character import Character
    from app.models.lora import LoraModel
    from app.services import generations as gen_service

    pid = env["project_id"]
    with session_scope() as db:
        ch = Character(name="Hero", appearance_prompt="a hero", project_id=pid)
        db.add(ch)
        db.flush()
        lora = LoraModel(character_id=ch.id, trigger_word="hero1", status="failed")
        db.add(lora)
        db.flush()
        gen = Generation(
            project_id=pid,
            job_type="lora_train",
            status="failed",
            params={"lora_model_id": str(lora.id)},
        )
        db.add(gen)
        db.flush()
        lora.generation_id = gen.id
        gid, lid = str(gen.id), str(lora.id)
    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    assert env["sent"] == [("q.p1.run_lora_train", (lid,))]
    assert _get(LoraModel, lid).status == "training"


def test_retry_only_from_failed(env):
    from app.services import generations as gen_service

    gid = _image_job(env["project_id"], status="succeeded")
    with session_scope() as db, pytest.raises(StudioError):
        gen_service.retry_job(db, uuid.UUID(gid))
    assert env["sent"] == []


def test_retry_of_uncertain_attempt_keeps_its_record(env):
    from app.services import generations as gen_service

    gid = _image_job(
        env["project_id"],
        status="failed",
        error_code="PROVIDER_UNCERTAIN",
        submit_state="submitted",
        provider_job_id="prov-1",
    )
    with session_scope() as db:
        db.add(
            GenerationAttempt(
                generation_id=uuid.UUID(gid),
                attempt_no=1,
                model_id="fake.model",
                provider_job_id="prov-1",
                error_code="PROVIDER_UNCERTAIN",
            )
        )
    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    gen = _get(Generation, gid)
    assert gen.status == "queued"
    assert gen.submit_state == "none" and gen.provider_job_id is None
    assert gen.exec_token is None
    attempts = _attempts(gid)
    assert [a.error_code for a in attempts] == ["PROVIDER_UNCERTAIN"]
    assert attempts[0].provider_job_id == "prov-1"

    # The next execution is a fresh attempt next to the uncertain one.
    _run_image(gid)
    attempts = _attempts(gid)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert _get(Generation, gid).status == "succeeded"


# --- P24: cancel cascades ------------------------------------------------------


def test_cancel_queued_video_releases_video_and_shot(env):
    from app.services import generations as gen_service

    pid = env["project_id"]
    shot_id = _shot(pid, status="video_generating", approved=True)
    gid, vid = _video_job(pid, shot_id)

    with session_scope() as db:
        gen_service.cancel_job(db, uuid.UUID(gid))
    assert _get(Generation, gid).status == "canceled"
    assert _get(Video, vid).status != "generating"
    assert _get(Shot, shot_id).status not in ("video_generating",)
    assert _get(Shot, shot_id).status == "frame_approved"


def test_cancel_running_job_stops_worker_and_cancels_provider(env):
    from app.services import generations as gen_service

    gid = _image_job(env["project_id"])
    fake = env["fake"]

    def _poll(pid):
        with session_scope() as db:
            gen_service.cancel_job(db, uuid.UUID(gid))
        return JobStatus(state="succeeded", progress_pct=100.0)

    fake.on_poll = _poll
    _run_image(gid)

    gen = _get(Generation, gid)
    assert gen.status == "canceled"  # success did not overwrite the cancel
    assert gen.output_path is None
    assert fake.cancels == ["fake-1"]


# --- H2 / P20: execution ownership ---------------------------------------------


def test_aba_stale_execution_cannot_write(env):
    from app.services import job_execution, job_recovery

    gid = _image_job(env["project_id"])
    a = job_execution.claim(Generation, uuid.UUID(gid))
    assert a is not None
    with session_scope() as db:
        db.get(Generation, uuid.UUID(gid)).lease_expires_at = _now() - timedelta(
            seconds=1
        )

    job_recovery.reconcile_once(_now())
    assert _get(Generation, gid).status == "queued"
    assert env["sent"] == [("q.image.run_generation", (gid,))]

    b = job_execution.claim(Generation, uuid.UUID(gid))
    assert b is not None and b.token != a.token

    late = job_execution.guarded_update(
        a, {"status": "succeeded", "output_path": "projects/x/late.png"}
    )
    assert late is False
    gen = _get(Generation, gid)
    assert gen.status == "running"
    assert gen.exec_token == b.token
    assert gen.output_path is None


def test_concurrent_deliveries_claim_once(env):
    fake = env["fake"]
    fake.submit_delay = 0.3
    gid = _image_job(env["project_id"])
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _deliver():
        try:
            barrier.wait()
            _run_image(gid)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert fake.submits == 1
    assert len(_attempts(gid)) == 1
    assert _get(Generation, gid).status == "succeeded"


def test_redelivery_of_succeeded_job_never_submits(env):
    gid = _image_job(env["project_id"])
    _run_image(gid)
    assert env["fake"].submits == 1
    assert _get(Generation, gid).status == "succeeded"

    _run_image(gid)  # broker redelivery (acks_late) of a finished job
    assert env["fake"].submits == 1
    assert len(_attempts(gid)) == 1


# --- dispatch robustness -------------------------------------------------------


def test_dispatch_after_commit_runs_every_callback(env):
    from app.db.session import dispatch_after_commit

    ran: list[str] = []

    def _boom():
        raise RuntimeError("broker down")

    with session_scope() as db:
        dispatch_after_commit(db, _boom)
        dispatch_after_commit(db, lambda: ran.append("second"))
    assert ran == ["second"]


# --- H1: reconcile -------------------------------------------------------------


def _expire_running(gid, submit_state):
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(gid))
        gen.status = "running"
        gen.exec_token = uuid.uuid4()
        gen.submit_state = submit_state
        gen.lease_expires_at = _now() - timedelta(seconds=5)


def test_reconcile_expired_intent_fails_uncertain(env):
    from app.services import job_recovery

    gid = _image_job(env["project_id"])
    _expire_running(gid, "intent")
    job_recovery.reconcile_once(_now())
    gen = _get(Generation, gid)
    assert gen.status == "failed"
    assert gen.error_code == "PROVIDER_UNCERTAIN"
    assert env["sent"] == []


def test_reconcile_expired_unsubmitted_requeues_and_dispatches(env):
    from app.services import job_recovery

    gid = _image_job(env["project_id"])
    _expire_running(gid, "none")
    job_recovery.reconcile_once(_now())
    gen = _get(Generation, gid)
    assert gen.status == "queued"
    assert gen.exec_token is None
    assert env["sent"] == [("q.image.run_generation", (gid,))]


def test_reconcile_redispatches_stale_queued_once(env):
    from app.services import job_recovery

    gid = _image_job(env["project_id"])
    stale = _now() - timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC + 5)
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(gid))
        gen.queued_at = stale
        gen.dispatched_at = stale
    now = _now()
    job_recovery.reconcile_once(now)
    job_recovery.reconcile_once(now + timedelta(seconds=1))
    assert env["sent"] == [("q.image.run_generation", (gid,))]
    # Fresh queued rows are left alone.
    fresh = _image_job(env["project_id"])
    job_recovery.reconcile_once(_now())
    assert ("q.image.run_generation", (fresh,)) not in env["sent"]


# --- RH1 / D7: uncertain provider submit ---------------------------------------


class _TimeoutAfterSend:
    """MockTransport handler: the submit request reaches the provider, then
    the response is lost (ReadTimeout)."""

    def __init__(self) -> None:
        self.submits = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/generate":
            self.submits += 1
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(404, json={})


class _RefusedBeforeSend:
    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        raise httpx.ConnectError("connection refused", request=request)


def _install_http(monkeypatch, handler):
    from app.models_lib.adapters.http_polling import HttpPollingAdapter

    model = {
        "model_id": "fake.model",
        "modality": "image",
        "provider_id": "http_polling",
        "status": "active",
        "pricing": {"unit": "per_image", "amount_usd": "0.01"},
        "capabilities": {
            "http": {
                "timeout_sec": 1,
                "submit": {
                    "url": "https://provider.test/v1/generate",
                    "body_template": {"prompt": "{prompt}"},
                },
                "job_id_path": "id",
                "status": {
                    "url_template": "https://provider.test/v1/jobs/{job_id}",
                    "state_path": "state",
                },
                "result": {"output_url_path": "output.0"},
            }
        },
    }
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)
    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(registry._ADAPTERS, "http_polling", adapter)
    return adapter


def test_read_timeout_after_submit_is_uncertain_and_never_resubmitted(env, monkeypatch):
    from app.services import job_recovery

    handler = _TimeoutAfterSend()
    _install_http(monkeypatch, handler)
    gid = _image_job(env["project_id"])

    _run_image(gid)
    gen = _get(Generation, gid)
    assert gen.status == "failed"
    assert gen.error_code == "PROVIDER_UNCERTAIN"
    assert handler.submits == 1
    assert [a.error_code for a in _attempts(gid)] == ["PROVIDER_UNCERTAIN"]

    # Neither reconcile nor a broker redelivery resubmits it.
    job_recovery.reconcile_once(_now() + timedelta(seconds=defaults.LEASE_TTL_SEC * 10))
    _run_image(gid)
    assert handler.submits == 1
    assert env["sent"] == []
    assert _get(Generation, gid).status == "failed"


def test_connect_refused_before_send_uses_normal_retry(env, monkeypatch):
    handler = _RefusedBeforeSend()
    _install_http(monkeypatch, handler)
    gid = _image_job(env["project_id"])

    _run_image(gid)
    gen = _get(Generation, gid)
    assert gen.status == "failed"
    assert gen.error_code == ErrorCode.PROVIDER_ERROR
    assert gen.retry_count == defaults.RETRY_MAX_ATTEMPTS
    assert handler.attempts == defaults.RETRY_MAX_ATTEMPTS + 1


# --- RH2 / D9: multishot fans out exactly once ------------------------------------


def _multishot_set(env) -> tuple[str, uuid.UUID]:
    from app.services import multishot as multishot_service

    shot_id = _shot(env["project_id"], status="frame_approved", approved=True)
    with session_scope() as db:
        result = multishot_service.request_coverage(db, shot_id, model_id="fake.model")
    env["sent"].clear()  # only the worker's dispatches are counted below
    return str(result["multishot_set_id"]), shot_id


def _coverage_rows(set_id: str) -> tuple[int, int, int]:
    """(coverage shots, child generations, frames linked) for one set."""
    from app.models.multishot import MultishotFrame

    with session_scope() as db:
        frames = (
            db.query(MultishotFrame)
            .filter(MultishotFrame.multishot_set_id == uuid.UUID(set_id))
            .all()
        )
        frame_ids = {f.id for f in frames}
        shots = (
            db.query(Shot).filter(Shot.coverage_source_frame_id.in_(frame_ids)).count()
        )
        gens = [
            g
            for g in db.query(Generation).filter(Generation.job_type == "text_to_image")
            if (g.params or {}).get("_multishot_frame_id")
            in {str(fid) for fid in frame_ids}
        ]
        linked = sum(1 for f in frames if f.generation_id and f.shot_id)
        return shots, len(gens), linked


def _image_dispatches(env) -> list[str]:
    return [a[0] for name, a in env["sent"] if name == "q.image.run_generation"]


def test_multishot_concurrent_deliveries_fan_out_once(env, monkeypatch):
    from app.services import multishot as multishot_service

    set_id, _ = _multishot_set(env)
    barrier = threading.Barrier(2)
    real = multishot_service.run_multishot

    def _synced(db, sid):
        barrier.wait(timeout=10)  # both deliveries race for the claim
        return real(db, sid)

    monkeypatch.setattr(multishot_service, "run_multishot", _synced)
    errors: list[BaseException] = []

    def _deliver():
        try:
            tasks_p1.run_multishot.apply(args=[set_id], throw=True)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert _coverage_rows(set_id) == (6, 6, 6)
    dispatched = _image_dispatches(env)
    assert len(dispatched) == 6 and len(set(dispatched)) == 6


def test_multishot_redelivery_after_partial_dispatch_failure_adds_nothing(
    env, monkeypatch
):
    from app.models.multishot import MultishotSet
    from app.services import job_recovery

    set_id, _ = _multishot_set(env)
    calls = {"n": 0}

    def _flaky(*args):
        calls["n"] += 1
        if calls["n"] in (2, 5):
            raise ConnectionError("broker hiccup")
        env["sent"].append(("q.image.run_generation", args))

    monkeypatch.setitem(celery_mod._JOB_DISPATCHERS, "q.image.run_generation", _flaky)
    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    assert calls["n"] == 6  # every callback attempted despite failures
    assert len(_image_dispatches(env)) == 4
    assert _coverage_rows(set_id) == (6, 6, 6)
    assert _get(MultishotSet, set_id).status == "running"

    # Broker redelivers the orchestrator message: nothing new is created.
    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    assert _coverage_rows(set_id) == (6, 6, 6)
    assert calls["n"] == 6

    # Reconcile re-sends the EXISTING queued children; it never fans out.
    later = _now() + timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC + 5)
    job_recovery.reconcile_once(later)
    assert _coverage_rows(set_id) == (6, 6, 6)
    assert calls["n"] == 12
    assert ("q.p1.run_multishot", (set_id,)) not in env["sent"]


def test_multishot_reconcile_redispatches_unclaimed_set_and_reaggregates(env):
    from app.models.multishot import MultishotFrame, MultishotSet
    from app.services import job_recovery

    set_id, _ = _multishot_set(env)
    later = _now() + timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC + 5)
    summary = job_recovery.reconcile_once(later)
    assert summary["multishot_redispatched"] == 1
    assert env["sent"] == [("q.p1.run_multishot", (set_id,))]
    assert _coverage_rows(set_id) == (0, 0, 0)  # reconcile itself creates nothing

    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    assert _coverage_rows(set_id) == (6, 6, 6)

    # Children finish but the per-frame hook was lost: frames stay pending.
    with session_scope() as db:
        frames = (
            db.query(MultishotFrame)
            .filter(MultishotFrame.multishot_set_id == uuid.UUID(set_id))
            .all()
        )
        for i, frame in enumerate(frames):
            gen = db.get(Generation, frame.generation_id)
            gen.status = "succeeded" if i else "failed"
    summary = job_recovery.reconcile_once(later)
    assert summary["multishot_aggregated"] == 1
    assert _get(MultishotSet, set_id).status == "partial"
    assert _coverage_rows(set_id) == (6, 6, 6)


def test_style_extract_redelivery_never_completes_another_kits_ledger(env):
    from app.models.style import StyleKit

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit_b = StyleKit(project_id=pid, name="B")  # older kit, queued first
        kit_a = StyleKit(project_id=pid, name="A")
        db.add_all([kit_b, kit_a])
        db.flush()
        kit_a_id, kit_b_id = kit_a.id, kit_b.id
    res_b, _ = _request_style(env, kit_b_id)
    res_a, msg_a = _request_style(env, kit_a_id)
    ledger_a_id, ledger_b_id = str(res_a["job_id"]), str(res_b["job_id"])

    tasks_p1.run_style_extract.apply(args=[msg_a], throw=True)
    assert _get(Generation, ledger_a_id).status == "succeeded"
    assert _get(Generation, ledger_b_id).status == "queued"
    version = _get(StyleKit, kit_a_id).version

    tasks_p1.run_style_extract.apply(args=[msg_a], throw=True)  # redelivery
    assert _get(Generation, ledger_b_id).status == "queued"
    assert _get(StyleKit, kit_a_id).version == version
    assert _get(StyleKit, kit_b_id).version == 1


# --- B4A-H1: per-attempt immutable outputs never block a manual retry --------------


def _fail_once(monkeypatch, module, name):
    """Make module.name raise once (bookkeeping error after the file save)."""
    real = getattr(module, name)
    state = {"n": 0}

    def _flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("injected bookkeeping failure")
        return real(*a, **k)

    monkeypatch.setattr(module, name, _flaky)
    return state


def _files_under(root, pattern):
    return sorted(p for p in root.rglob(pattern) if p.is_file())


def test_audio_lost_attempt_file_never_blocks_manual_retry(env, monkeypatch):
    """P01: A's lease expires after fetch -> reconcile PROVIDER_UNCERTAIN ->
    manual retry -> A resumes and saves its file (CAS 0 rows, orphan) -> B
    succeeds with its own file."""
    from app.media import ffmpeg_ops
    from app.services import generations as gen_service
    from app.services import job_recovery

    monkeypatch.setattr(ffmpeg_ops, "probe", lambda *a, **k: {"duration_sec": 1.0})
    with session_scope() as db:
        row = AudioGeneration(
            project_id=env["project_id"], kind="dialogue", text="hi",
            model_id="fake.model", status="queued",
        )
        db.add(row)
        db.flush()
        aid = str(row.id)

    fake = env["fake"]
    real_save = tasks_audio._save_wav
    state = {"n": 0}

    def _save(*a, **k):
        # A has fetched the result and passed its last ownership check; it
        # stalls here past its lease (process pause) before saving.
        state["n"] += 1
        if state["n"] == 1:
            later = _now() + timedelta(seconds=defaults.LEASE_TTL_SEC * 2)
            assert job_recovery.reconcile_once(later)["uncertain"] == 1
            assert _get(AudioGeneration, aid).error_code == "PROVIDER_UNCERTAIN"
            with session_scope() as db:
                gen_service.retry_job(db, uuid.UUID(aid))
        return real_save(*a, **k)

    monkeypatch.setattr(tasks_audio, "_save_wav", _save)
    a = tasks_audio.run_generation.apply(args=[aid])
    assert a.result["status"] == "lost"
    a_files = _files_under(env["tmp"], "*.wav")
    assert len(a_files) == 1  # A's orphan

    b = tasks_audio.run_generation.apply(args=[aid])
    assert b.successful(), b.result
    row = _get(AudioGeneration, aid)
    assert row.status == "done", (row.error_code, row.error_message)
    assert row.output_path and (env["tmp"] / row.output_path).is_file()
    assert env["tmp"] / row.output_path not in a_files
    assert fake.submits == 2


def test_image_bookkeeping_error_after_save_then_retry_succeeds(env, monkeypatch):
    """P09: the file is saved, then the success transaction fails -> failed is
    committed; the manual retry saves a NEW attempt file and succeeds."""
    from app.services import generations as gen_service
    from app.services import job_execution

    _fail_once(monkeypatch, job_execution, "finish_attempt")
    gid = _image_job(env["project_id"])
    _run_image(gid)
    gen = _get(Generation, gid)
    assert gen.status == "failed" and gen.output_path is None
    orphans = _files_under(env["tmp"], "*.png")
    assert len(orphans) == 1

    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    _run_image(gid)
    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    assert (env["tmp"] / gen.output_path).is_file()
    assert env["tmp"] / gen.output_path not in orphans
    assert [a.attempt_no for a in _attempts(gid)] == [1, 2]


def test_character_master_bookkeeping_error_then_retry_succeeds(env, monkeypatch):
    from app.models.character import Character, CharacterReference
    from app.services import generations as gen_service
    from app.services import job_execution

    pid = env["project_id"]
    with session_scope() as db:
        ch = Character(name="Hero", appearance_prompt="a hero", project_id=pid)
        db.add(ch)
        db.flush()
        cid = ch.id
    gid = _image_job(pid, params={
        "_character_id": str(cid), "_reference_role": "master",
        "_source": "generated", "_character_dir": f"projects/{pid}/characters/{cid}",
    })
    _fail_once(monkeypatch, job_execution, "finish_attempt")
    _run_image(gid)
    assert _get(Generation, gid).status == "failed"
    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    _run_image(gid)
    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    with session_scope() as db:
        refs = db.query(CharacterReference).filter(
            CharacterReference.character_id == cid).all()
        assert len(refs) == 1 and refs[0].is_primary
        assert refs[0].image_path == gen.output_path
    assert (env["tmp"] / gen.output_path).is_file()


def test_video_bookkeeping_error_after_save_then_retry_succeeds(env, monkeypatch):
    from app.media import ffmpeg_ops
    from app.services import generations as gen_service
    from app.services import job_execution

    monkeypatch.setattr(ffmpeg_ops, "probe", lambda *a, **k: {"duration_sec": 5.0})

    def _no_frame(*a, **k):
        raise StudioError(ErrorCode.VALIDATION_ERROR, "no frame in test")

    monkeypatch.setattr(ffmpeg_ops, "extract_frame", _no_frame)
    pid = env["project_id"]
    shot_id = _shot(pid, status="video_generating", approved=True)
    gid, vid = _video_job(pid, shot_id)
    _fail_once(monkeypatch, job_execution, "finish_attempt")
    tasks_video.run_generation.apply(args=[gid])
    assert _get(Generation, gid).status == "failed"
    assert _get(Video, vid).status == "failed"
    orphans = _files_under(env["tmp"], "*.mp4")
    assert len(orphans) == 1

    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(gid))
    tasks_video.run_generation.apply(args=[gid])
    gen = _get(Generation, gid)
    assert gen.status == "succeeded", (gen.error_code, gen.error_message)
    video = _get(Video, vid)
    assert video.status == "done" and video.output_path == gen.output_path
    assert env["tmp"] / video.output_path not in orphans
    assert _get(Shot, shot_id).status == "video_done"


# --- B4A-H2: style extraction is a stable job row -----------------------------------


def _style_model():
    from app.services import style_extract as style_service

    with session_scope() as db:
        if db.get(Model, style_service.EXTRACT_MODEL_ID) is None:
            db.add(Model(
                model_id=style_service.EXTRACT_MODEL_ID, modality="text",
                provider_id="stub", capabilities={}, constraints={},
                pricing_unit="per_call", pricing_amount_usd=0.01,
            ))


def _request_style(env, kit_id) -> tuple[dict, str]:
    from app.services import style_extract as style_service

    before = len(env["sent"])
    with session_scope() as db:
        res = style_service.request_extract(db, kit_id, source_asset_ids=[])
    msgs = [a[0] for name, a in env["sent"][before:] if name == "q.p1.run_style_extract"]
    assert len(msgs) == 1, env["sent"]
    return res, msgs[0]


def _style_ledgers(pid) -> list[Generation]:
    with session_scope() as db:
        rows = (
            db.query(Generation)
            .filter(Generation.project_id == pid, Generation.job_type == "style_extract")
            .order_by(Generation.created_at, Generation.queued_at)
            .all()
        )
        db.expunge_all()
        return rows


def test_style_extract_redelivered_message_never_completes_a_newer_extraction(env):
    """P02: A done, B queued for the SAME kit, A's message redelivered -> B
    untouched and the kit version unchanged."""
    from app.models.style import StyleKit

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit = StyleKit(project_id=pid, name="K")
        db.add(kit)
        db.flush()
        kit_id = kit.id
    res_a, msg_a = _request_style(env, kit_id)
    tasks_p1.run_style_extract.apply(args=[msg_a], throw=True)
    assert _get(StyleKit, kit_id).version == 2
    assert [g.status for g in _style_ledgers(pid)] == ["succeeded"]

    res_b, msg_b = _request_style(env, kit_id)
    tasks_p1.run_style_extract.apply(args=[msg_a], throw=True)  # redelivery of A
    assert [g.status for g in _style_ledgers(pid)] == ["succeeded", "queued"]
    assert _get(StyleKit, kit_id).version == 2

    tasks_p1.run_style_extract.apply(args=[msg_b], throw=True)  # B's own message
    assert [g.status for g in _style_ledgers(pid)] == ["succeeded", "succeeded"]
    assert _get(StyleKit, kit_id).version == 3


def test_style_extract_global_kit_same_message_twice_applies_once(env):
    """P08: a global kit (no project, no cost ledger) still has a job row."""
    from app.models.style import StyleKit

    _style_model()
    with session_scope() as db:
        kit = StyleKit(project_id=None, name="global")
        db.add(kit)
        db.flush()
        kit_id = kit.id
    _, msg = _request_style(env, kit_id)
    tasks_p1.run_style_extract.apply(args=[msg], throw=True)
    tasks_p1.run_style_extract.apply(args=[msg], throw=True)
    assert _get(StyleKit, kit_id).version == 2


def test_style_extract_applies_its_own_source_snapshot(env):
    from app.models.asset import Asset
    from app.models.style import StyleKit
    from app.services import style_extract as style_service

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit = StyleKit(project_id=pid, name="K")
        db.add(kit)
        assets = [Asset(project_id=pid, kind="image", name=f"a{i}",
                        image_path=f"projects/{pid}/assets/a{i}.png") for i in range(4)]
        db.add_all(assets)
        db.flush()
        kit_id, aids = kit.id, [a.id for a in assets]
    before = len(env["sent"])
    with session_scope() as db:
        style_service.request_extract(db, kit_id, source_asset_ids=aids[:3])
    with session_scope() as db:
        style_service.request_extract(db, kit_id, source_asset_ids=aids[3:])
    msgs = [a[0] for n, a in env["sent"][before:] if n == "q.p1.run_style_extract"]
    tasks_p1.run_style_extract.apply(args=[msgs[0]], throw=True)
    assert _get(StyleKit, kit_id).extraction_source_ids == [str(a) for a in aids[:3]]
    tasks_p1.run_style_extract.apply(args=[msgs[1]], throw=True)
    assert _get(StyleKit, kit_id).extraction_source_ids == [str(aids[3])]


def test_style_extract_cancel_crash_and_retry_follow_the_job_row(env, monkeypatch):
    from app.models.style import StyleExtraction, StyleKit
    from app.services import generations as gen_service
    from app.services import style_extract as style_service

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit = StyleKit(project_id=pid, name="K")
        db.add(kit)
        db.flush()
        kit_id = kit.id
    # Canceled before the worker ran: the message is a no-op.
    res, msg = _request_style(env, kit_id)
    with session_scope() as db:
        gen_service.cancel_job(db, res["job_id"])
    assert _get(StyleExtraction, msg).status == "canceled"
    tasks_p1.run_style_extract.apply(args=[msg])
    assert _get(StyleKit, kit_id).version == 1

    # Crash inside the transaction: rolled back, then committed as failed.
    res, msg = _request_style(env, kit_id)
    stub = style_service.STUB_EXTRACTION
    monkeypatch.setattr(style_service, "STUB_EXTRACTION", None)
    assert tasks_p1.run_style_extract.apply(args=[msg]).failed()
    assert _get(StyleExtraction, msg).status == "failed"
    assert _get(Generation, msg).status == "failed"
    assert _get(StyleKit, kit_id).version == 1
    monkeypatch.setattr(style_service, "STUB_EXTRACTION", stub)

    # Manual retry of the ledger re-queues the same job row and dispatches it.
    before = len(env["sent"])
    with session_scope() as db:
        gen_service.retry_job(db, uuid.UUID(msg))
    assert env["sent"][before:] == [("q.p1.run_style_extract", (msg,))]
    assert _get(StyleExtraction, msg).status == "queued"
    tasks_p1.run_style_extract.apply(args=[msg], throw=True)
    assert _get(StyleExtraction, msg).status == "succeeded"
    assert _get(Generation, msg).status == "succeeded"
    assert _get(StyleKit, kit_id).version == 2


# --- B4A-H3: batch parent / shot aggregation converges -----------------------------


def _batch(pid, shot_id, n=2) -> tuple[str, list[str]]:
    with session_scope() as db:
        parent = Generation(project_id=pid, shot_id=shot_id, job_type="text_to_image",
                            model_id="fake.model", status="running", is_batch=True)
        db.add(parent)
        db.flush()
        kids = []
        for i in range(n):
            child = Generation(project_id=pid, shot_id=shot_id, job_type="text_to_image",
                               model_id="fake.model", status="queued",
                               parent_generation_id=parent.id, batch_index=i)
            db.add(child)
            db.flush()
            kids.append(str(child.id))
        return str(parent.id), kids


@pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)
def test_pg_concurrent_child_terminal_commits_converge_parent_and_shot(env):
    """P07: both children's success UPDATEs are in flight before either
    aggregates; the parent/shot must still end terminal."""
    from app.services import job_execution

    pid = env["project_id"]
    shot_id = _shot(pid, status="frame_generating")
    parent_id, kids = _batch(pid, shot_id)
    execs = [job_execution.claim(Generation, uuid.UUID(k)) for k in kids]
    assert all(execs)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[bool] = []

    def _finish(ex):
        def _after(db, gen):
            barrier.wait(timeout=20)  # both child UPDATEs uncommitted here
            job_execution.on_generation_terminal(db, gen, success=True)

        try:
            results.append(job_execution.guarded_update(
                ex, {"status": "succeeded", "finished_at": _now()}, after=_after))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_finish, args=(ex,)) for ex in execs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert results == [True, True]
    assert [_get(Generation, k).status for k in kids] == ["succeeded", "succeeded"]
    assert _get(Generation, parent_id).status == "succeeded"
    assert _get(Shot, shot_id).status == "frame_done"


def test_reconcile_converges_stuck_batch_parent_and_shot(env):
    from app.services import job_recovery

    pid = env["project_id"]
    shot_id = _shot(pid, status="frame_generating")
    parent_id, kids = _batch(pid, shot_id)
    with session_scope() as db:
        for k, st in zip(kids, ("succeeded", "failed")):
            db.get(Generation, uuid.UUID(k)).status = st
    # A still-running batch is left alone.
    other_shot = _shot(pid, status="frame_generating")
    other_parent, other_kids = _batch(pid, other_shot)
    with session_scope() as db:
        db.get(Generation, uuid.UUID(other_kids[0])).status = "succeeded"

    summary = job_recovery.reconcile_once(_now())
    assert summary["batch_parents_aggregated"] == 1
    assert _get(Generation, parent_id).status == "failed"
    assert _get(Shot, shot_id).status == "frame_done"
    assert _get(Generation, other_parent).status == "running"
    assert _get(Shot, other_shot).status == "frame_generating"
    assert job_recovery.reconcile_once(_now())["batch_parents_aggregated"] == 0


# --- B4A-M1: running rows from before execution tracking ---------------------------


def test_reconcile_fails_legacy_running_rows_without_lease(env):
    from app.services import job_recovery

    pid = env["project_id"]
    old = _now() - timedelta(days=1)
    legacy = _image_job(pid, status="running", started_at=old)
    fresh = _image_job(pid, status="running", started_at=_now())
    with session_scope() as db:
        audio = AudioGeneration(project_id=pid, kind="dialogue", text="hi",
                                status="running", created_at=old)
        db.add(audio)
        db.flush()
        aid = str(audio.id)
    # A batch parent without a lease is NOT a legacy execution.
    shot_id = _shot(pid, status="frame_generating")
    parent_id, kids = _batch(pid, shot_id)
    with session_scope() as db:
        db.get(Generation, uuid.UUID(parent_id)).started_at = old

    summary = job_recovery.reconcile_once(_now())
    gen = _get(Generation, legacy)
    assert gen.status == "failed" and gen.error_code == "PROVIDER_UNCERTAIN"
    assert _get(AudioGeneration, aid).status == "failed"
    assert _get(AudioGeneration, aid).error_code == "PROVIDER_UNCERTAIN"
    assert _get(Generation, fresh).status == "running"
    assert _get(Generation, parent_id).status == "running"
    assert summary["uncertain"] == 2
    assert env["sent"] == []  # never auto-resubmitted


# --- B4b-fix: multishot set aggregation (B4B-H5, B4B-M3) ---------------------------


def _set_frames(set_id: str) -> list:
    from app.models.multishot import MultishotFrame

    with session_scope() as db:
        rows = (
            db.query(MultishotFrame)
            .filter(MultishotFrame.multishot_set_id == uuid.UUID(set_id))
            .order_by(MultishotFrame.created_at, MultishotFrame.id)
            .all()
        )
        db.expunge_all()
        return rows


@pytest.mark.parametrize(
    "states,expected",
    [
        (["done"] * 6, "done"),
        (["done"] * 3 + ["failed"] * 3, "partial"),
        (["failed"] * 6, "failed"),
    ],
)
def test_multishot_reconcile_converges_running_set_with_terminal_frames(
    env, states, expected
):
    """B4B-H5 (P06 result state): every frame is terminal but the set stayed
    `running` (the last two hooks raced). Reconcile must aggregate the set
    from the full frame state, once."""
    from app.models.multishot import MultishotFrame, MultishotSet
    from app.services import job_recovery

    set_id, _ = _multishot_set(env)
    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    with session_scope() as db:
        for frame, state in zip(_set_frames(set_id), states):
            row = db.get(MultishotFrame, frame.id)
            row.status = state
            gen = db.get(Generation, frame.generation_id)
            gen.status = "succeeded" if state == "done" else "failed"
    assert _get(MultishotSet, set_id).status == "running"

    later = _now() + timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC + 5)
    summary = job_recovery.reconcile_once(later)
    assert summary["multishot_aggregated"] == 1
    assert _get(MultishotSet, set_id).status == expected
    assert job_recovery.reconcile_once(later)["multishot_aggregated"] == 0
    assert _coverage_rows(set_id) == (6, 6, 6)


@pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)
def test_pg_last_two_multishot_frame_hooks_converge(env):
    """B4B-H5 (P06): the last two frame hooks write their frames, then both
    read the set's frames before either commits. The set must still end
    terminal (set aggregation is serialized on the set row)."""
    from sqlalchemy import event

    from app.models.multishot import MultishotSet
    from app.services import job_execution

    set_id, _ = _multishot_set(env)
    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    gens = [str(f.generation_id) for f in _set_frames(set_id)]

    def _succeed(gid):
        ex = job_execution.claim(Generation, uuid.UUID(gid))
        assert ex is not None
        return job_execution.guarded_update(
            ex,
            {"status": "succeeded", "finished_at": _now()},
            after=lambda db, gen: job_execution.on_generation_terminal(
                db, gen, success=True
            ),
        )

    for gid in gens[:4]:
        assert _succeed(gid)

    barrier = threading.Barrier(2)
    engine = sess_mod.get_engine()

    def _sync(conn, cursor, statement, params, context, executemany):
        name = threading.current_thread().name
        if (
            name.startswith("hook-")
            and statement.lstrip().upper().startswith("SELECT")
            and "FROM multishot_frames" in statement
            and "multishot_frames.multishot_set_id" in statement
        ):
            try:  # both frame writes in flight before either aggregates
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass  # the other hook is (correctly) blocked on the set lock

    event.listen(engine, "before_cursor_execute", _sync)
    errors: list[BaseException] = []
    results: list[bool] = []

    def _hook(gid):
        try:
            results.append(_succeed(gid))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=_hook, args=(gid,), name=f"hook-{i}")
            for i, gid in enumerate(gens[4:])
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        event.remove(engine, "before_cursor_execute", _sync)
    assert not errors, errors
    assert results == [True, True]
    assert [f.status for f in _set_frames(set_id)] == ["done"] * 6
    assert _get(MultishotSet, set_id).status == "done"


def test_multishot_eager_children_last_event_is_completed(env, monkeypatch):
    """B4B-M3 (P07): with children that run synchronously at dispatch, the
    set's 30% `running` progress event must not arrive after its
    job.completed."""
    from app.core import sse_bridge
    from app.models.multishot import MultishotSet

    events: list[tuple] = []
    real_publish = sse_bridge.SseBridge.publish

    def _record(self, channel, event, data):
        events.append((event, str(data.get("job_id")), data.get("status")))
        return real_publish(self, channel, event, data)

    monkeypatch.setattr(sse_bridge.SseBridge, "publish", _record)
    set_id, _ = _multishot_set(env)
    monkeypatch.setitem(
        celery_mod._JOB_DISPATCHERS,
        "q.image.run_generation",
        lambda gid: tasks_image.run_generation.apply(args=[gid], throw=True),
    )
    tasks_p1.run_multishot.apply(args=[set_id], throw=True)
    assert _get(MultishotSet, set_id).status == "done"
    mine = [(ev, st) for ev, job, st in events if job == set_id]
    assert ("job.completed", "done") in mine, mine
    assert mine[-1] == ("job.completed", "done"), mine
    assert ("job.progress", "running") in mine, mine


# --- R2-C1: the test suite never resets a non-test database ------------------------


def test_test_db_guard_refuses_service_like_databases():
    from tests import conftest

    for name in ("t", "hiway", "postgres", "studio", "", None):
        assert not conftest.is_dedicated_test_db_name(name), name
    for name in ("hiway_test", "test", "studio_TEST"):
        assert conftest.is_dedicated_test_db_name(name), name
    # Refused BEFORE any connection: port 1 would raise if it connected.
    with pytest.raises(pytest.fail.Exception, match="not a dedicated test"):
        conftest.reset_test_postgres("postgresql+psycopg://u:p@127.0.0.1:1/t")


def _run_suite_subset(tmp_path, env_overrides: dict, node_ids: list[str]):
    import subprocess
    import sys
    from pathlib import Path

    api_dir = Path(__file__).resolve().parent.parent
    child_env = {
        k: v for k, v in os.environ.items() if k not in ("TEST_DATABASE_URL",)
    }
    child_env.update(env_overrides)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    tmp_path.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'child'}", *node_ids],
        cwd=api_dir, env=child_env, capture_output=True, text=True, timeout=600,
    )


_SUBSET = [
    "tests/test_job_lifecycle.py::test_terminal_failure_persists_in_new_session",
    "tests/test_render_postflight.py::test_request_render_while_rendering_is_locked",
    "tests/test_job_lifecycle.py::test_pg_last_two_multishot_frame_hooks_converge",
]


def test_service_database_url_is_never_used_by_db_fixtures(tmp_path):
    """R2-C1: a service-like DATABASE_URL (as `docker compose run api pytest`
    inherits from .env) is ignored — the fixtures run on temp SQLite (this
    one is unreachable, so any use of it would error) and PG-only tests skip."""
    result = _run_suite_subset(
        tmp_path,
        {"DATABASE_URL": "postgresql+psycopg://svc:svc@127.0.0.1:1/t"},
        _SUBSET,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "2 passed, 1 skipped" in out, out[-3000:]
    assert "TEST_DATABASE_URL" in out  # the skip reason names the opt-in


@pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs a Postgres server (set TEST_DATABASE_URL to a dedicated *_test DB)",
)
def test_service_like_postgres_database_survives_the_suite(tmp_path):
    """R2-C1 on a real server: a service-like database (name `t`-style, with a
    sentinel table) given as DATABASE_URL is untouched; given as
    TEST_DATABASE_URL the fixtures fail clearly and still never reset it."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import configured_test_database_url

    test_url = make_url(configured_test_database_url())
    service_url = test_url.set(database="hiway_service_like")
    admin = create_engine(test_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists_ = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'hiway_service_like'")
        ).first()
        if not exists_:
            conn.execute(text("CREATE DATABASE hiway_service_like"))
    admin.dispose()
    service = create_engine(service_url)
    with service.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS sentinel (v int)"))
        conn.execute(text("DELETE FROM sentinel"))
        conn.execute(text("INSERT INTO sentinel VALUES (42)"))
    url_str = service_url.render_as_string(hide_password=False)
    try:
        as_service = _run_suite_subset(tmp_path / "a", {"DATABASE_URL": url_str}, _SUBSET)
        out = as_service.stdout + as_service.stderr
        assert as_service.returncode == 0, out[-3000:]
        assert "2 passed, 1 skipped" in out, out[-3000:]

        as_test = _run_suite_subset(
            tmp_path / "b",
            {"DATABASE_URL": url_str, "TEST_DATABASE_URL": url_str},
            _SUBSET[:2],
        )
        out = as_test.stdout + as_test.stderr
        assert as_test.returncode != 0, out[-3000:]
        assert "not a dedicated test database" in out, out[-3000:]

        with service.connect() as conn:
            assert conn.execute(text("SELECT v FROM sentinel")).scalar_one() == 42
    finally:
        service.dispose()


# --- B4a-r2: pending transitions lock the shot, style lock order, cancel re-read ---

_PG_ONLY = pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)


def _take(pid, shot_id, *, status) -> str:
    """A standalone (non-batch) image take of a shot."""
    with session_scope() as db:
        gen = Generation(
            project_id=pid, shot_id=shot_id, job_type="text_to_image",
            model_id="fake.model", status=status,
            error_code="PROVIDER_ERROR" if status == "failed" else None,
        )
        db.add(gen)
        db.flush()
        return str(gen.id)


def _hold_retry_while(target_gid: str, other) -> list[BaseException]:
    """R retries `target_gid` and holds its commit while `other` runs in a
    second thread (given ~1.5 s to finish before R commits)."""
    from app.services import generations as gen_service

    r_ready, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def _retry():
        try:
            with session_scope() as db:
                gen_service.retry_job(db, uuid.UUID(target_gid))
                r_ready.set()
                release.wait(timeout=30)
        except BaseException as exc:  # pragma: no cover - surfaced by caller
            errors.append(exc)
            r_ready.set()

    def _other():
        try:
            other()
        except BaseException as exc:  # pragma: no cover - surfaced by caller
            errors.append(exc)

    rt = threading.Thread(target=_retry)
    rt.start()
    assert r_ready.wait(timeout=30)
    ot = threading.Thread(target=_other)
    ot.start()
    ot.join(timeout=1.5)  # unlocked shot: `other` commits here, before R
    release.set()
    rt.join(timeout=60)
    ot.join(timeout=60)
    return errors


@_PG_ONLY
def test_pg_frame_retry_pending_vs_other_take_terminal_keeps_shot_generating(env):
    """R2-H1 (P01): A running, B failed on one shot. R re-queues B and holds
    its commit; A's terminal aggregation must not settle the shot from B's
    stale `failed` state (the retry locks the shot)."""
    from app.services import job_execution, job_recovery

    pid = env["project_id"]
    shot_id = _shot(pid, status="frame_generating")
    a = _take(pid, shot_id, status="queued")
    b = _take(pid, shot_id, status="failed")
    ex = job_execution.claim(Generation, uuid.UUID(a))
    assert ex is not None

    def _a_terminal():
        assert job_execution.guarded_update(
            ex, {"status": "succeeded", "finished_at": _now()},
            after=lambda db, gen: job_execution.on_generation_terminal(
                db, gen, success=True),
        )

    errors = _hold_retry_while(b, _a_terminal)
    assert not errors, errors
    assert _get(Generation, a).status == "succeeded"
    assert _get(Generation, b).status == "queued"
    assert _get(Shot, shot_id).status == "frame_generating"
    job_recovery.reconcile_once(_now())
    assert _get(Shot, shot_id).status == "frame_generating"


@_PG_ONLY
def test_pg_video_retry_pending_vs_other_video_terminal_keeps_shot_generating(env):
    """R2-H1, video path: the retried video job's shot stays video_generating."""
    from app.services import job_execution

    pid = env["project_id"]
    shot_id = _shot(pid, status="video_generating", approved=True)
    a, _ = _video_job(pid, shot_id)
    b, vid_b = _video_job(pid, shot_id, status="failed", video_status="failed")
    ex = job_execution.claim(Generation, uuid.UUID(a))
    assert ex is not None

    def _a_terminal():
        assert job_execution.guarded_update(
            ex,
            {"status": "failed", "error_code": "PROVIDER_ERROR", "finished_at": _now()},
            after=lambda db, gen: job_execution.on_generation_terminal(
                db, gen, success=False),
        )

    errors = _hold_retry_while(b, _a_terminal)
    assert not errors, errors
    assert _get(Generation, a).status == "failed"
    assert _get(Generation, b).status == "queued"
    assert _get(Video, vid_b).status == "generating"
    assert _get(Shot, shot_id).status == "video_generating"


def test_reconcile_repairs_shot_settled_while_a_job_is_pending(env):
    """R2-H1: reconcile repairs shot/child disagreement in BOTH directions —
    also a settled shot (frame_done / frame_approved) with a pending job."""
    from app.services import job_recovery

    pid = env["project_id"]
    frame_shot = _shot(pid, status="frame_done")
    _take(pid, frame_shot, status="succeeded")
    _take(pid, frame_shot, status="queued")
    video_shot = _shot(pid, status="frame_approved", approved=True)
    _video_job(pid, video_shot)
    calm = _shot(pid, status="frame_done")
    _take(pid, calm, status="succeeded")

    summary = job_recovery.reconcile_once(_now())
    assert summary["shots_recomputed"] == 2
    assert _get(Shot, frame_shot).status == "frame_generating"
    assert _get(Shot, video_shot).status == "video_generating"
    assert _get(Shot, calm).status == "frame_done"
    assert job_recovery.reconcile_once(_now())["shots_recomputed"] == 0


@_PG_ONLY
def test_pg_style_worker_vs_cancel_never_deadlocks(env):
    """R2-M1 (P04): worker completion and ledger cancel take their first row
    lock, then meet. One lock order (ledger -> extraction -> kit) -> no 40P01
    and one consistent terminal outcome."""
    from sqlalchemy import event

    from app.models.style import StyleExtraction, StyleKit
    from app.services import generations as gen_service
    from app.services import style_extract as style_service

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit = StyleKit(project_id=pid, name="K")
        db.add(kit)
        db.flush()
        kit_id = kit.id
    _, msg = _request_style(env, kit_id)

    barrier = threading.Barrier(2)
    engine = sess_mod.get_engine()
    locked: set[str] = set()

    def _sync(conn, cursor, statement, params, context, executemany):
        name = threading.current_thread().name
        if not name.startswith("sx-") or name in locked:
            return
        s = statement.lstrip().upper()
        if s.startswith("UPDATE") or " FOR UPDATE" in s or " FOR NO KEY UPDATE" in s:
            locked.add(name)
            try:  # both first locks taken before either goes on
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass  # the other side is (correctly) blocked on our lock

    outcomes: dict[str, object] = {}

    def _worker():
        try:
            with session_scope() as db:
                outcomes["worker"] = style_service.complete_extract(db, msg)["status"]
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcomes["worker"] = exc

    def _cancel():
        try:
            with session_scope() as db:
                gen_service.cancel_job(db, uuid.UUID(msg))
            outcomes["cancel"] = "canceled"
        except StudioError as exc:
            outcomes["cancel"] = exc.code
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcomes["cancel"] = exc

    event.listen(engine, "after_cursor_execute", _sync)
    try:
        threads = [threading.Thread(target=_worker, name="sx-worker"),
                   threading.Thread(target=_cancel, name="sx-cancel")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        event.remove(engine, "after_cursor_execute", _sync)
    assert not any(isinstance(v, BaseException) for v in outcomes.values()), outcomes
    final = (_get(Generation, msg).status, _get(StyleExtraction, msg).status,
             _get(StyleKit, kit_id).version)
    assert final in {("canceled", "canceled", 1), ("succeeded", "succeeded", 2)}, final
    if final[0] == "canceled":
        assert outcomes == {"cancel": "canceled", "worker": "canceled"}, outcomes
    else:
        assert outcomes == {"cancel": ErrorCode.VALIDATION_ERROR, "worker": "succeeded"}


def test_reconcile_recovers_global_style_extractions(env):
    """R2-M2 (P02): a global kit's extraction (no cost ledger) whose dispatch
    was lost is re-sent by reconcile (the claim keeps it single-run); a
    never-picked-up or stranded running one ends failed. A project kit's
    extraction is still recovered through its ledger only (no double send)."""
    from app.models.style import StyleExtraction, StyleKit
    from app.services import job_recovery

    _style_model()
    with session_scope() as db:
        gkit = StyleKit(project_id=None, name="global")
        pkit = StyleKit(project_id=env["project_id"], name="P")
        db.add_all([gkit, pkit])
        db.flush()
        gkit_id, pkit_id = gkit.id, pkit.id
    _, msg = _request_style(env, gkit_id)
    _, pmsg = _request_style(env, pkit_id)
    env["sent"].clear()  # the broker lost both messages

    def _style_sent():
        return sorted(a[0] for n, a in env["sent"] if n == "q.p1.run_style_extract")

    job_recovery.reconcile_once(_now())
    assert _style_sent() == []  # too fresh
    later = _now() + timedelta(seconds=defaults.RECONCILE_REDISPATCH_AFTER_SEC + 5)
    summary = job_recovery.reconcile_once(later)
    assert summary["style_redispatched"] == 1
    assert _style_sent() == sorted([msg, pmsg])
    tasks_p1.run_style_extract.apply(args=[msg], throw=True)
    tasks_p1.run_style_extract.apply(args=[msg], throw=True)  # duplicate: no-op
    assert _get(StyleExtraction, msg).status == "succeeded"
    assert _get(StyleKit, gkit_id).version == 2
    env["sent"].clear()
    job_recovery.reconcile_once(later)
    assert _style_sent() == []  # done; the ledger was re-sent in this window

    _, stranded = _request_style(env, gkit_id)
    _, never = _request_style(env, gkit_id)
    with session_scope() as db:
        db.get(StyleExtraction, uuid.UUID(stranded)).status = "running"
    stuck = _now() + timedelta(
        seconds=max(defaults.RECONCILE_P1_STUCK_AFTER_SEC, defaults.LEASE_TTL_SEC) + 5
    )
    summary = job_recovery.reconcile_once(stuck)
    assert summary["style_failed"] == 2
    for jid in (stranded, never):
        row = _get(StyleExtraction, jid)
        assert row.status == "failed" and row.error_code == "PROVIDER_ERROR", jid
    assert _get(StyleKit, gkit_id).version == 2


def test_retry_of_legacy_style_ledger_without_job_row_is_rejected(env):
    """R2-M3 (P03): a failed style ledger from before migration 0014 has no
    style_extractions row; retrying it must fail clearly (not a silent no-op)."""
    from app.models.style import StyleKit
    from app.services import generations as gen_service
    from app.services import style_extract as style_service

    _style_model()
    pid = env["project_id"]
    with session_scope() as db:
        kit = StyleKit(project_id=pid, name="K")
        db.add(kit)
        db.flush()
        ledger = Generation(
            project_id=pid, job_type="style_extract",
            model_id=style_service.EXTRACT_MODEL_ID, status="failed",
            error_code="PROVIDER_ERROR", params={"style_kit_id": str(kit.id)},
        )
        db.add(ledger)
        db.flush()
        lid = ledger.id
    before = len(env["sent"])
    with pytest.raises(StudioError) as err:
        with session_scope() as db:
            gen_service.retry_job(db, lid)
    assert err.value.code == ErrorCode.VALIDATION_ERROR
    assert "re-request" in err.value.message.lower()
    assert _get(Generation, lid).status == "failed"
    assert env["sent"][before:] == []


def test_cancel_batch_while_a_child_succeeds_concurrently_ends_canceled(
    env, monkeypatch
):
    """R2-M4 (P05): C1 commits success after cancel read it as running (its
    cancel CAS hits 0 rows); C2 is canceled. The parent decision re-reads the
    children: [succeeded, canceled] -> parent canceled (cancel rule), not failed."""
    from app.services import generations as gen_service
    from app.services import job_execution

    pid = env["project_id"]
    shot_id = _shot(pid, status="frame_generating")
    parent_id, kids = _batch(pid, shot_id)
    ex = job_execution.claim(Generation, uuid.UUID(kids[0]))
    assert ex is not None
    real = gen_service._cancel_values
    fired: list[int] = []

    def _racing(now):
        if not fired:  # C1's success commits before any cancel CAS runs
            fired.append(1)
            assert job_execution.guarded_update(
                ex, {"status": "succeeded", "finished_at": _now()},
                after=lambda db, gen: job_execution.on_generation_terminal(
                    db, gen, success=True),
            )
        return real(now)

    monkeypatch.setattr(gen_service, "_cancel_values", _racing)
    with session_scope() as db:
        gen_service.cancel_job(db, uuid.UUID(parent_id))
    assert fired
    assert [_get(Generation, k).status for k in kids] == ["succeeded", "canceled"]
    assert _get(Generation, parent_id).status == "canceled"
    assert _get(Shot, shot_id).status == "frame_done"


# --- B4R3-M1: enqueue clears frame_stale only for the prompt inputs it used ----


def _shot_with_take(pid, *, action: str, stale: bool) -> uuid.UUID:
    """A shot with one committed take (so a prompt edit marks it stale)."""
    shot_id = _shot(pid, status="frame_done")
    _take(pid, shot_id, status="succeeded")
    with session_scope() as db:
        shot = db.get(Shot, shot_id)
        shot.action = action
        shot.frame_stale = stale
    return shot_id


def _parent_prompt(job_id) -> str:
    return _get(Generation, job_id).prompt_final or ""


def test_enqueue_frame_clears_stale_when_no_edit_intervenes(env):
    """Normal path: a fresh frame for the current prompt resolves the warning."""
    from app.services import generations as gen_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking_old_action", stale=True)
    with session_scope() as db:
        out = gen_service.enqueue_frame(db, shot_id, model_id="fake.model")
    shot = _get(Shot, shot_id)
    assert shot.frame_stale is False
    assert shot.status == "frame_generating"
    assert "walking_old_action" in _parent_prompt(out["job_id"])


def _edit_between_assembly_and_shot_lock(monkeypatch, edit) -> list[int]:
    """Run `edit(db)` in another committed transaction right before enqueue's
    final Shot lock (mark_frame_shot_pending), i.e. after prompt assembly."""
    from app.services import generations as gen_service

    real = gen_service.mark_frame_shot_pending
    fired: list[int] = []

    def _hooked(db, shot_id, **kw):
        if not fired:
            fired.append(1)
            errors: list[BaseException] = []

            def _other():
                try:
                    with session_scope() as db2:
                        edit(db2, shot_id)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            t = threading.Thread(target=_other)
            t.start()
            t.join(timeout=30)
            assert not t.is_alive(), "the concurrent edit blocked on the enqueue"
            assert not errors, errors
        return real(db, shot_id, **kw)

    monkeypatch.setattr(gen_service, "mark_frame_shot_pending", _hooked)
    return fired


@_PG_ONLY
def test_pg_shot_edit_between_assembly_and_shot_lock_keeps_frame_stale(
    env, monkeypatch
):
    """B4R3-M1 (P04): the stored prompt is the pre-edit one, so the edit's
    stale warning must survive the enqueue's final Shot lock."""
    from app.services import generations as gen_service
    from app.services import scene as scene_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking_old_action", stale=False)
    fired = _edit_between_assembly_and_shot_lock(
        monkeypatch,
        lambda db, sid: scene_service.update_shot(
            db, sid, {"action": "running_new_action"}),
    )
    with session_scope() as db:
        out = gen_service.enqueue_frame(db, shot_id, model_id="fake.model")
    assert fired
    shot = _get(Shot, shot_id)
    assert shot.action == "running_new_action"
    assert "walking_old_action" in _parent_prompt(out["job_id"])
    assert shot.frame_stale is True  # the edit's warning is kept
    assert shot.status == "frame_generating"  # the pending transition still ran


@_PG_ONLY
def test_pg_stale_mark_without_shot_field_change_between_assembly_and_lock_is_kept(
    env, monkeypatch
):
    """Another invalidation (e.g. a character appearance edit) sets only
    frame_stale between assembly and the Shot lock: it is kept too."""
    from app.services import generations as gen_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking_old_action", stale=False)

    def _mark(db, sid):
        db.get(Shot, sid).frame_stale = True

    fired = _edit_between_assembly_and_shot_lock(monkeypatch, _mark)
    with session_scope() as db:
        gen_service.enqueue_frame(db, shot_id, model_id="fake.model")
    assert fired
    assert _get(Shot, shot_id).frame_stale is True


@_PG_ONLY
def test_pg_shot_edit_during_variations_enqueue_keeps_frame_stale(env, monkeypatch):
    """Same rule on the variations path (also assembles the shot prompt)."""
    from app.services import generations as gen_service
    from app.services import scene as scene_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking_old_action", stale=True)
    fired = _edit_between_assembly_and_shot_lock(
        monkeypatch,
        lambda db, sid: scene_service.update_shot(
            db, sid, {"action": "running_new_action"}),
    )
    with session_scope() as db:
        gen_service.enqueue_variations(db, shot_id, count=2, model_id="fake.model")
    assert fired
    assert _get(Shot, shot_id).frame_stale is True


# --- B4R4-M1: a new invalidation of an already-stale shot is not lost ---------


def _hero_on(pid, shot_id, appearance: str) -> uuid.UUID:
    """A character with `appearance`, referenced by the shot (primary)."""
    from app.models.character import Character
    from app.models.production import ShotReference

    with session_scope() as db:
        char = Character(project_id=pid, name="hero", appearance_prompt=appearance)
        db.add(char)
        db.flush()
        db.add(ShotReference(shot_id=shot_id, character_id=char.id, purpose="primary"))
        return char.id


def test_character_appearance_edit_bumps_the_invalidation_revision(env):
    """Every invalidation increments Shot.frame_stale_rev, also when the
    shot is already stale (the bool alone cannot tell two marks apart)."""
    from app.services import character as character_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking", stale=True)
    cid = _hero_on(pid, shot_id, "old_blue_coat")
    rev0 = _get(Shot, shot_id).frame_stale_rev
    with session_scope() as db:
        character_service.update_character(db, cid, {"appearance_prompt": "new_red_coat"})
    with session_scope() as db:
        character_service.update_character(db, cid, {"appearance_prompt": "green_coat"})
    shot = _get(Shot, shot_id)
    assert shot.frame_stale is True
    assert shot.frame_stale_rev == rev0 + 2


@_PG_ONLY
def test_pg_character_edit_on_already_stale_shot_between_assembly_and_lock_keeps_stale(
    env, monkeypatch
):
    """B4R4-M1 (P02): initial frame_stale=True, the enqueue assembles the
    prompt with the old appearance, then a Character appearance edit commits
    before the enqueue's Shot lock. The stored prompt is the old one, so the
    shot must stay stale (stale True -> True is a NEW invalidation)."""
    from app.models.character import Character
    from app.services import character as character_service
    from app.services import generations as gen_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking", stale=True)
    cid = _hero_on(pid, shot_id, "old_blue_coat")
    fired = _edit_between_assembly_and_shot_lock(
        monkeypatch,
        lambda db, sid: character_service.update_character(
            db, cid, {"appearance_prompt": "new_red_coat"}),
    )
    with session_scope() as db:
        out = gen_service.enqueue_frame(db, shot_id, model_id="fake.model")
    assert fired
    prompt = _parent_prompt(out["job_id"])
    assert "old_blue_coat" in prompt and "new_red_coat" not in prompt
    character = _get(Character, cid)
    assert (character.appearance_prompt, character.version) == ("new_red_coat", 2)
    shot = _get(Shot, shot_id)
    assert shot.frame_stale is True  # the character edit's warning is kept
    assert shot.status == "frame_generating"


def test_invalidating_an_already_stale_shot_rewrites_the_flag(env):
    """R5-M1: when the invalidating session already loaded frame_stale=True, the
    UPDATE must still carry frame_stale=TRUE together with the revision bump.
    Otherwise a concurrent enqueue that committed False in between survives and
    the new invalidation is lost (the reverse commit order of R4-M1)."""
    from sqlalchemy import event

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking", stale=True)
    statements: list[str] = []
    with session_scope() as db:
        shot = db.get(Shot, shot_id)
        assert shot.frame_stale is True  # loaded as True in this session

        def _capture(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE SHOTS"):
                statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            # A concurrent enqueue clears the flag in another transaction.
            with session_scope() as other:
                other.get(Shot, shot_id).frame_stale = False
            shot.mark_frame_stale()
            db.flush()
        finally:
            event.remove(engine, "before_cursor_execute", _capture)
    assert statements, "invalidation issued no UPDATE"
    assert "frame_stale=" in statements[-1].replace(" ", "")
    assert _get(Shot, shot_id).frame_stale is True


def test_shot_prompt_edit_bumps_the_invalidation_revision(env):
    """scene.update_shot invalidates through Shot.mark_frame_stale(), so a
    prompt edit on an already-stale shot is still a NEW invalidation (rev bump)
    that an in-flight enqueue cannot clear (B4R4-M1 / R5-M1, B6 integration)."""
    from app.services import scene as scene_service

    pid = env["project_id"]
    shot_id = _shot_with_take(pid, action="walking", stale=True)
    rev0 = _get(Shot, shot_id).frame_stale_rev
    with session_scope() as db:
        scene_service.update_shot(db, shot_id, {"action": "running"})
    with session_scope() as db:
        scene_service.update_shot(db, shot_id, {"action": "jumping"})
    with session_scope() as db:
        # A non-prompt edit invalidates nothing.
        scene_service.update_shot(db, shot_id, {"action": "jumping"})
    shot = _get(Shot, shot_id)
    assert shot.frame_stale is True
    assert shot.frame_stale_rev == rev0 + 2
