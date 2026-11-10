"""Budget reservation + DB idempotency (B5, plan D1/D2/D7; G8 F07/H3, F06/M1, RH1).

Proves:
  * queued/running jobs reserve budget: a second request that would push
    committed spend past the budget is refused (sequential and, on Postgres,
    concurrent with real row locks); batch parents are not double counted;
    failed/canceled rows release; PROVIDER_UNCERTAIN keeps its reservation
    and a manual retry reserves the new attempt on top of it (RH1);
  * LLM calls commit a pending reservation before the external call and
    settle as succeeded / uncertain (kept) / failed (released);
  * Idempotency-Key is stored in the DB per (method, actual path, key):
    replay is identical and creates nothing, a different body is 409, two
    paths are independent, replay survives a restart, an expired record
    starts over, and (Postgres) two concurrent same-key requests create one
    job;
  * cost.alert is published once when the warn ratio is crossed.

Runs on a per-test SQLite file by default; with TEST_DATABASE_URL pointing at
a dedicated Postgres test database the same tests run there and the
concurrency tests are enabled (R2-C1 guard in conftest).
"""

from __future__ import annotations

import importlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
import app.main  # noqa: F401  (imports every model module for create_all)
from app.core import defaults
from app.core.celery_app import _JOB_DISPATCHERS
from app.core.config import get_settings
from app.core.middleware import register_middleware
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.audio import AudioGeneration
from app.models.generation import Generation
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.system import LLMCall, Model
from app.models_lib import registry
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter
from app.routers import generations as generations_router
from app.services import budget_guard
from tests.conftest import isolated_engine, postgres_tests_enabled

IMAGE_MODEL = "stub.t2i-fast"
TEXT_MODEL = "fake.text"
TASKS = (
    "q.image.run_generation",
    "q.video.run_generation",
    "q.audio.run_generation",
    "q.p1.run_lora_train",
    "q.p1.run_style_extract",
    "q.p1.run_multishot",
)
_PG_ONLY = pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)


def _make_app() -> FastAPI:
    api = FastAPI()
    register_middleware(api)
    api.include_router(generations_router.router, prefix="/api/v1")
    return api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url, engine = isolated_engine(tmp_path, "b5.db")
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

    apply_seed()
    sent: list[tuple[str, tuple]] = []
    for name in TASKS:
        monkeypatch.setitem(
            _JOB_DISPATCHERS, name, lambda *a, _n=name: sent.append((_n, a))
        )
    monkeypatch.setattr(defaults, "POLL_INTERVAL_START_SEC", 0.0)
    alerts: list[dict] = []
    monkeypatch.setattr(
        budget_guard,
        "get_bridge",
        lambda: type(
            "B",
            (),
            {
                "publish": lambda self, ch, ev, data: alerts.append(
                    {"event": ev, **data}
                )
            },
        )(),
    )
    with session_scope() as db:
        project = Project(title="b5", monthly_budget_usd=Decimal("10"))
        db.add(project)
        db.flush()
        pid = project.id
    _price(Decimal("6"))
    yield {
        "project_id": pid,
        "client": TestClient(_make_app()),
        "sent": sent,
        "alerts": alerts,
        "engine": engine,
    }
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()
    engine.dispose()


# --- helpers -----------------------------------------------------------------


def _price(amount: Decimal, model_id: str = IMAGE_MODEL) -> None:
    with session_scope() as db:
        db.get(Model, model_id).pricing_amount_usd = amount


def _budget(pid, amount) -> None:
    with session_scope() as db:
        db.get(Project, pid).monthly_budget_usd = (
            None if amount is None else Decimal(str(amount))
        )


def _shot(pid) -> uuid.UUID:
    with session_scope() as db:
        scene = Scene(project_id=pid, order_index=0)
        db.add(scene)
        db.flush()
        shot = Shot(scene_id=scene.id, order_index=0, action="a cat on a roof")
        db.add(shot)
        db.flush()
        return shot.id


def _frame(client, shot_id, *, key: str | None = None, batch_size: int = 1):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/api/v1/shots/{shot_id}/frame",
        json={"batch_size": batch_size, "model_id": IMAGE_MODEL},
        headers=headers,
    )


def _committed(pid) -> tuple[Decimal, Decimal]:
    with session_scope() as db:
        return budget_guard.committed_usd(db, pid)


def _row(model, row_id):
    with session_scope() as db:
        row = db.get(model, uuid.UUID(str(row_id)))
        db.expunge_all()
        return row


def _count(model, *criteria) -> int:
    with session_scope() as db:
        return db.query(model).filter(*criteria).count()


def _failed_image(pid, *, cost="6", error_code="PROVIDER_UNCERTAIN") -> str:
    with session_scope() as db:
        gen = Generation(
            project_id=pid,
            job_type="text_to_image",
            model_id=IMAGE_MODEL,
            prompt_final="a cat",
            status="failed",
            error_code=error_code,
            submit_state="submitted",
            provider_job_id="prov-1",
            estimated_cost_usd=Decimal(cost),
        )
        db.add(gen)
        db.flush()
        return str(gen.id)


def _holds(pid) -> list:
    from app.models.system import BudgetHold

    with session_scope() as db:
        rows = db.query(BudgetHold).filter(BudgetHold.project_id == pid).all()
        db.expunge_all()
        return rows


# --- 1. F07: queued jobs reserve budget (different keys, sequential) ----------


def test_sequential_requests_see_queued_reservations(env):
    pid, client = env["project_id"], env["client"]
    shot = _shot(pid)
    r1 = _frame(client, shot, key="key-a")
    assert r1.status_code == 202, r1.text
    r2 = _frame(client, _shot(pid), key="key-b")
    assert r2.status_code == 403, r2.text
    assert r2.json()["code"] == "BUDGET_EXCEEDED"
    assert (
        _count(Generation, Generation.project_id == pid, Generation.is_batch.is_(False))
        == 1
    )
    assert _committed(pid) == (Decimal(0), Decimal("6"))


# --- 2. F07/H3: two different-key requests at once (Postgres row locks) ------


@_PG_ONLY
def test_pg_concurrent_different_keys_accept_exactly_one(env, monkeypatch):
    pid = env["project_id"]
    shots = [_shot(pid), _shot(pid)]
    barrier = threading.Barrier(2)
    # Force overlap: after reading committed spend each request waits for the
    # other one to read too. Under the project lock the second request cannot
    # read before the first commits, so the wait times out and they serialize.
    name = "committed_usd" if hasattr(budget_guard, "committed_usd") else "spent_usd"
    real = getattr(budget_guard, name)

    def _slow_read(*args, **kwargs):
        value = real(*args, **kwargs)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return value

    monkeypatch.setattr(budget_guard, name, _slow_read)
    results: list[int] = []

    def _post(shot_id, key):
        results.append(_frame(TestClient(_make_app()), shot_id, key=key).status_code)

    threads = [
        threading.Thread(target=_post, args=(s, f"k-{i}")) for i, s in enumerate(shots)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == [202, 403], results
    assert (
        _count(Generation, Generation.project_id == pid, Generation.is_batch.is_(False))
        == 1
    )


# --- 3. batch parent counted once ---------------------------------------------


def test_batch_parent_is_not_double_counted(env):
    pid, client = env["project_id"], env["client"]
    _price(Decimal("2"))
    shot = _shot(pid)
    # 2 x $2 reserved through the children; the $4 parent does not count.
    assert _frame(client, shot, key="b1", batch_size=2).status_code == 202
    # 4 reserved + 4 new = 8 < 10 (with the parent counted it would be 12).
    r = _frame(client, shot, key="b2", batch_size=2)
    assert r.status_code == 202, r.text
    with session_scope() as db:
        for gen in db.query(Generation).filter(Generation.project_id == pid):
            gen.status = "succeeded"
    with session_scope() as db:
        assert budget_guard.spent_usd(db, pid) == Decimal("8")  # parents excluded
        assert budget_guard.summary(db, pid)["total_actual_usd"] == pytest.approx(8.0)
    assert _committed(pid) == (Decimal("8"), Decimal(0))


def test_queued_multishot_set_reserves_until_fan_out(env):
    """A multishot request creates no frame jobs until the worker fans out:
    the queued set itself must hold the coverage estimate (6 angles)."""
    from app.models.multishot import COVERAGE_ANGLES, MultishotSet

    pid, client = env["project_id"], env["client"]
    _price(Decimal("1"))
    base = _shot(pid)
    with session_scope() as db:
        frame = Generation(
            project_id=pid,
            shot_id=base,
            job_type="text_to_image",
            model_id=IMAGE_MODEL,
            status="succeeded",
            output_path="x.png",
            actual_cost_usd=Decimal(0),
        )
        db.add(frame)
        db.flush()
        db.add(
            MultishotSet(
                shot_id=base,
                source_frame_generation_id=frame.id,
                state_snapshot={"model_id": IMAGE_MODEL},
                status="queued",
            )
        )
    assert _committed(pid) == (Decimal(0), Decimal(len(COVERAGE_ANGLES)))
    r = _frame(client, base, key="m1", batch_size=4)  # 6 + 4 >= 10
    assert r.status_code == 403, r.text
    with session_scope() as db:
        db.query(MultishotSet).update({MultishotSet.status: "failed"})
    assert _committed(pid) == (Decimal(0), Decimal(0))


# --- 4. RH1: manual retry of an uncertain attempt -----------------------------


def test_retry_of_uncertain_attempt_keeps_its_reservation(env):
    pid, client = env["project_id"], env["client"]
    gid = _failed_image(pid)

    r = client.post(f"/api/v1/jobs/{gid}/retry")
    assert r.status_code == 403, r.text  # 6 held + 6 new >= 10
    assert r.json()["code"] == "BUDGET_EXCEEDED"
    gen = _row(Generation, gid)
    assert gen.status == "failed" and gen.error_code == "PROVIDER_UNCERTAIN"
    assert gen.provider_job_id == "prov-1"
    assert _holds(pid) == []
    assert env["sent"] == []
    assert _committed(pid) == (Decimal(0), Decimal("6"))  # uncertain = reserved

    _budget(pid, 20)
    r = client.post(f"/api/v1/jobs/{gid}/retry")
    assert r.status_code == 202, r.text
    assert _row(Generation, gid).status == "queued"
    holds = _holds(pid)
    assert [(h.source_kind, str(h.source_id), h.amount_usd) for h in holds] == [
        ("generation", gid, Decimal("6"))
    ]
    assert _committed(pid) == (Decimal(0), Decimal("12"))
    # The re-queued attempt finishing does not release the old attempt's hold.
    with session_scope() as db:
        db.get(Generation, uuid.UUID(gid)).status = "succeeded"
    assert _committed(pid) == (Decimal("6"), Decimal("6"))


def test_retry_of_uncertain_audio_keeps_its_reservation(env):
    from app.services import generations as gen_service

    pid = env["project_id"]
    with session_scope() as db:
        row = AudioGeneration(
            project_id=pid,
            kind="dialogue",
            text="hi",
            model_id="stub.tts",
            status="failed",
            error_code="PROVIDER_UNCERTAIN",
            estimated_cost_usd=Decimal("6"),
        )
        db.add(row)
        db.flush()
        aid = row.id
    with session_scope() as db, pytest.raises(Exception) as exc:
        gen_service.retry_job(db, aid)
    assert getattr(exc.value, "code", None) == "BUDGET_EXCEEDED"
    assert _row(AudioGeneration, aid).status == "failed"
    assert _holds(pid) == []
    _budget(pid, 20)
    with session_scope() as db:
        gen_service.retry_job(db, aid)
    assert _row(AudioGeneration, aid).status == "queued"
    assert [h.source_kind for h in _holds(pid)] == ["audio"]
    assert _committed(pid) == (Decimal(0), Decimal("12"))


# --- 5. a normal failed retry re-reserves -------------------------------------


def test_retry_of_failed_job_reserves_again(env):
    pid, client = env["project_id"], env["client"]
    assert _frame(client, _shot(pid), key="q1").status_code == 202  # $6 queued
    gid = _failed_image(pid, error_code="PROVIDER_ERROR")
    r = client.post(f"/api/v1/jobs/{gid}/retry")
    assert r.status_code == 403, r.text  # 6 queued + 6 new
    assert _committed(pid) == (Decimal(0), Decimal("6"))  # plain failure released
    gen = _row(Generation, gid)
    assert gen.status == "failed" and gen.error_code == "PROVIDER_ERROR"
    assert _holds(pid) == []
    _budget(pid, 13)
    r = client.post(f"/api/v1/jobs/{gid}/retry")
    assert r.status_code == 202, r.text
    assert _committed(pid) == (Decimal(0), Decimal("12"))
    assert _holds(pid) == []  # nothing uncertain to carry over


# --- 6. failed / canceled release ---------------------------------------------


def test_canceled_job_releases_its_reservation(env):
    pid, client = env["project_id"], env["client"]
    shot = _shot(pid)
    r1 = _frame(client, shot, key="c1")
    assert r1.status_code == 202
    assert _frame(client, shot, key="c2").status_code == 403  # 6 reserved + 6
    parent = r1.json()["job_id"]
    assert client.post(f"/api/v1/jobs/{parent}/cancel").status_code == 200
    assert _committed(pid) == (Decimal(0), Decimal(0))
    r3 = _frame(client, shot, key="c3")
    assert r3.status_code == 202, r3.text
    with session_scope() as db:
        child = (
            db.query(Generation)
            .filter(Generation.parent_generation_id == uuid.UUID(r3.json()["job_id"]))
            .one()
        )
        child.status = "failed"
        child.error_code = "PROVIDER_ERROR"
    assert _committed(pid) == (Decimal(0), Decimal(0))
    assert _frame(client, shot, key="c4").status_code == 202


# --- 7. LLM reservation -------------------------------------------------------


class FakeLLM(ProviderAdapter):
    provider_id = "fakellm"

    def __init__(self) -> None:
        self.seen: list[list[str | None]] = []
        self.submit_error: BaseException | None = None

    def submit(self, model_id: str, payload: dict) -> str:
        # "In flight": another session must already see the reservation.
        with session_scope() as other:
            self.seen.append([c.status for c in other.query(LLMCall).all()])
        if self.submit_error is not None:
            raise self.submit_error
        return "llm-1"

    def poll(self, provider_job_id: str) -> JobStatus:
        return JobStatus(state="succeeded", progress_pct=100.0)

    def cancel(self, provider_job_id: str) -> None:
        pass

    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        return EstimatedCost(Decimal("0"))

    def fetch_result(self, provider_job_id: str) -> bytes:
        return b"A synopsis."


@pytest.fixture()
def llm(env, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setitem(registry._ADAPTERS, "fakellm", fake)
    with session_scope() as db:
        db.add(
            Model(
                model_id=TEXT_MODEL,
                modality="text",
                provider_id="fakellm",
                capabilities={},
                constraints={},
                pricing_unit="per_1k_tokens",
                pricing_amount_usd=Decimal("1"),
            )
        )
    return fake


def _synopsis(pid):
    from app.services import story

    with session_scope() as db:
        return story.generate_synopsis(db, pid, "a cat on a roof", TEXT_MODEL)


def _llm_rows(pid) -> list[tuple[str | None, Decimal]]:
    with session_scope() as db:
        return [
            (c.status, c.estimated_cost_usd)
            for c in db.query(LLMCall)
            .filter(LLMCall.project_id == pid)
            .order_by(LLMCall.created_at)
        ]


def test_llm_call_reserves_before_the_external_call(env, llm):
    pid = env["project_id"]
    call_id, text = _synopsis(pid)
    assert text == "A synopsis."
    assert llm.seen == [["pending"]]  # committed + visible while in flight
    assert _llm_rows(pid) == [("succeeded", Decimal("1"))]
    assert _committed(pid) == (Decimal("1"), Decimal(0))


def test_llm_timeout_after_send_stays_reserved(env, llm):
    pid = env["project_id"]
    llm.submit_error = httpx.ReadTimeout("response lost")
    with pytest.raises(httpx.ReadTimeout):
        _synopsis(pid)
    assert llm.seen == [["pending"]]
    assert _llm_rows(pid) == [("uncertain", Decimal("1"))]
    assert _committed(pid) == (Decimal(0), Decimal("1"))


def test_llm_error_before_send_releases(env, llm):
    pid = env["project_id"]
    llm.submit_error = httpx.ConnectError("connection refused")
    with pytest.raises(httpx.ConnectError):
        _synopsis(pid)
    assert _llm_rows(pid) == [("failed", Decimal("1"))]
    assert _committed(pid) == (Decimal(0), Decimal(0))


def test_llm_call_over_budget_is_refused_before_sending(env, llm):
    pid = env["project_id"]
    _budget(pid, "1.5")
    llm.submit_error = httpx.ReadTimeout("response lost")
    with pytest.raises(httpx.ReadTimeout):
        _synopsis(pid)  # $1 uncertain stays reserved
    llm.submit_error = None
    with pytest.raises(Exception) as exc:
        _synopsis(pid)  # 1 + 1 >= 1.5
    assert getattr(exc.value, "code", None) == "BUDGET_EXCEEDED"
    assert len(llm.seen) == 1  # never sent
    assert _llm_rows(pid) == [("uncertain", Decimal("1"))]


# --- 8. F06/M1: DB idempotency -------------------------------------------------


def _parents(shot_id) -> int:
    return _count(
        Generation, Generation.shot_id == shot_id, Generation.is_batch.is_(True)
    )


def test_same_key_and_body_replays_identical_response(env):
    from app.models.system import IdempotencyRecord

    pid, client = env["project_id"], env["client"]
    _budget(pid, None)
    shot = _shot(pid)
    r1 = _frame(client, shot, key="same")
    r2 = _frame(client, shot, key="same")
    assert r1.status_code == r2.status_code == 202
    assert r2.json() == r1.json()
    assert _parents(shot) == 1
    assert len(env["sent"]) == 1  # the replay dispatched nothing
    with session_scope() as db:
        rec = db.query(IdempotencyRecord).one()
        assert rec.path == f"/api/v1/shots/{shot}/frame"  # actual path
        assert str(rec.job_id) == r1.json()["job_id"]


def test_same_key_with_different_body_is_conflict(env):
    pid, client = env["project_id"], env["client"]
    _budget(pid, None)
    shot = _shot(pid)
    assert _frame(client, shot, key="k").status_code == 202
    r = _frame(client, shot, key="k", batch_size=2)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert _parents(shot) == 1


def test_same_key_on_two_shots_creates_two_jobs(env):
    pid, client = env["project_id"], env["client"]
    _budget(pid, None)
    a, b = _shot(pid), _shot(pid)
    ra = _frame(client, a, key="shared")
    rb = _frame(client, b, key="shared")
    assert ra.status_code == rb.status_code == 202
    assert ra.json()["job_id"] != rb.json()["job_id"]
    assert _parents(a) == 1 and _parents(b) == 1


def test_replay_survives_an_api_restart(env):
    pid = env["project_id"]
    _budget(pid, None)
    shot = _shot(pid)
    r1 = _frame(env["client"], shot, key="restart")
    assert r1.status_code == 202
    # "Restart": fresh module state and a new app/client on the same DB.
    from app.services import idempotency

    importlib.reload(idempotency)
    importlib.reload(generations_router)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=env["engine"], expire_on_commit=False)
    r2 = _frame(TestClient(_make_app()), shot, key="restart")
    assert r2.status_code == 202
    assert r2.json() == r1.json()
    assert _parents(shot) == 1


def test_expired_record_starts_a_new_job(env):
    from app.models.system import IdempotencyRecord

    pid, client = env["project_id"], env["client"]
    _budget(pid, None)
    shot = _shot(pid)
    r1 = _frame(client, shot, key="old")
    with session_scope() as db:
        rec = db.query(IdempotencyRecord).one()
        rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    r2 = _frame(client, shot, key="old")
    assert r2.status_code == 202
    assert r2.json()["job_id"] != r1.json()["job_id"]
    assert _parents(shot) == 2
    with session_scope() as db:
        rec = db.query(IdempotencyRecord).one()
        assert str(rec.job_id) == r2.json()["job_id"]
    assert defaults.IDEMPOTENCY_TTL_SEC == 86400


def _concurrent_same_key(env, monkeypatch, key):
    """Both requests pass the record lookup before either inserts."""
    from app.services import idempotency

    pid = env["project_id"]
    shot = _shot(pid)
    barrier = threading.Barrier(2)
    name = "_find" if hasattr(idempotency, "_find") else "recall"
    real = getattr(idempotency, name)
    local = threading.local()

    def _lookup(*args, **kwargs):
        value = real(*args, **kwargs)
        if not getattr(local, "done", False):
            local.done = True
            barrier.wait(timeout=20)
        return value

    monkeypatch.setattr(idempotency, name, _lookup)
    responses: list[httpx.Response] = []

    def _post():
        responses.append(_frame(TestClient(_make_app()), shot, key=key))

    threads = [threading.Thread(target=_post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return shot, responses


@_PG_ONLY
def test_pg_concurrent_same_key_creates_one_job(env, monkeypatch):
    _budget(env["project_id"], None)  # the loser hits the unique constraint
    shot, responses = _concurrent_same_key(env, monkeypatch, "race")
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert _parents(shot) == 1
    assert _count(Generation, Generation.shot_id == shot) == 2  # parent + child


@_PG_ONLY
def test_pg_concurrent_same_key_under_budget_replays_winner(env, monkeypatch):
    # $6 of $10: the loser's create() sees the winner's reservation (403)
    # and must answer with the winner's response instead.
    shot, responses = _concurrent_same_key(env, monkeypatch, "race-budget")
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert _parents(shot) == 1


# --- 9. cost.alert once when crossing the warn ratio ---------------------------


def test_cost_alert_published_once_when_crossing_warn_ratio(env):
    pid, client = env["project_id"], env["client"]
    _price(Decimal("4"))
    shot = _shot(pid)
    assert _frame(client, shot, key="w1").status_code == 202  # 0 -> 40%
    assert env["alerts"] == []
    assert _frame(client, shot, key="w2").status_code == 202  # 40% -> 80%
    assert len(env["alerts"]) == 1, env["alerts"]
    alert = env["alerts"][0]
    assert alert["event"] == "cost.alert"
    assert alert["threshold_pct"] == round(defaults.BUDGET_WARN_RATIO * 100)
    assert alert["usage_pct"] == pytest.approx(80.0)
    assert alert["spent_usd"] == pytest.approx(8.0)
    _price(Decimal("1"))
    assert _frame(client, shot, key="w3").status_code == 202  # 80% -> 90%: no new alert
    assert len(env["alerts"]) == 1


# --- review B5-H1: retry reservations live in the CURRENT budget period --------


def _last_month() -> datetime:
    start, _ = budget_guard._month_bounds_utc()
    return start - timedelta(days=3)


def _last_month_failed_image(pid, *, cost="6", error_code="PROVIDER_ERROR") -> str:
    gid = _failed_image(pid, cost=cost, error_code=error_code)
    with session_scope() as db:
        gen = db.get(Generation, uuid.UUID(gid))
        gen.created_at = _last_month()
        gen.reserved_at = _last_month()
    return gid


def test_retry_of_last_month_job_is_reserved_this_month(env):
    """B5-H1: a retry approved this month must stay in this month's ledger
    even though the row was created last month (P01: two $6 retries on a $10
    budget were both accepted and committed stayed 0)."""
    pid, client = env["project_id"], env["client"]
    first = _last_month_failed_image(pid)
    second = _last_month_failed_image(pid)
    assert _committed(pid) == (Decimal(0), Decimal(0))  # last month's failures

    r = client.post(f"/api/v1/jobs/{first}/retry")
    assert r.status_code == 202, r.text
    assert _committed(pid) == (Decimal(0), Decimal("6"))  # the new attempt
    r = client.post(f"/api/v1/jobs/{second}/retry")
    assert r.status_code == 403, r.text  # 6 reserved + 6 new >= 10
    assert _row(Generation, second).status == "failed"


def test_retry_of_last_month_audio_is_reserved_this_month(env):
    from app.services import generations as gen_service

    pid = env["project_id"]
    ids = []
    for _ in range(2):
        with session_scope() as db:
            row = AudioGeneration(
                project_id=pid, kind="dialogue", text="hi", model_id="stub.tts",
                status="failed", error_code="PROVIDER_ERROR",
                estimated_cost_usd=Decimal("6"),
                created_at=_last_month(), reserved_at=_last_month(),
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
    with session_scope() as db:
        gen_service.retry_job(db, ids[0])
    assert _committed(pid) == (Decimal(0), Decimal("6"))
    with session_scope() as db, pytest.raises(Exception) as exc:
        gen_service.retry_job(db, ids[1])
    assert getattr(exc.value, "code", None) == "BUDGET_EXCEEDED"
    assert _row(AudioGeneration, ids[1]).status == "failed"


def test_uncertain_hold_keeps_the_period_of_the_held_attempt(env):
    """The hold carries the OLD attempt's budget period: last month's
    uncertain charge does not count against this month, the new attempt does."""
    pid, client = env["project_id"], env["client"]
    gid = _last_month_failed_image(pid, error_code="PROVIDER_UNCERTAIN")
    r = client.post(f"/api/v1/jobs/{gid}/retry")
    assert r.status_code == 202, r.text  # only the new $6 counts this month
    holds = _holds(pid)
    assert len(holds) == 1
    start, _end = budget_guard._month_bounds_utc()
    period = holds[0].period_at
    if period.tzinfo is None:
        period = period.replace(tzinfo=timezone.utc)
    assert period < start
    assert _committed(pid) == (Decimal(0), Decimal("6"))


# --- review B5-H2: multishot fan-out never slips between ledger reads ----------


@_PG_ONLY
def test_pg_multishot_fan_out_cannot_commit_inside_a_budget_check(env, monkeypatch):
    """P03: the API reads Generation rows, then (before it reads queued sets)
    the worker claims the set and fans it out into leaf jobs. With the worker
    sharing the budget lock (Project -> set) it cannot commit until the API
    transaction ends, so the $6 coverage reservation is never missed."""
    from app.models.multishot import COVERAGE_ANGLES, MultishotFrame, MultishotSet
    from app.services import multishot as multishot_service

    pid, client = env["project_id"], env["client"]
    _price(Decimal("1"))
    _budget(pid, 9)
    base = _shot(pid)
    with session_scope() as db:
        frame = Generation(
            project_id=pid, shot_id=base, job_type="text_to_image", model_id=IMAGE_MODEL,
            status="succeeded", output_path="x.png", actual_cost_usd=Decimal(0),
        )
        db.add(frame)
        db.flush()
        db.get(Shot, base).approved_frame_generation_id = frame.id
        mset = MultishotSet(
            shot_id=base, source_frame_generation_id=frame.id,
            state_snapshot={"model_id": IMAGE_MODEL}, status="queued",
        )
        db.add(mset)
        db.flush()
        for angle in COVERAGE_ANGLES:  # as multishot.request_multishot creates them
            db.add(MultishotFrame(multishot_set_id=mset.id, coverage_type=angle, status="pending"))
        set_id = str(mset.id)

    worker_done = threading.Event()
    worker_errors: list[BaseException] = []

    def _worker():
        try:
            with session_scope() as db:
                multishot_service.run_multishot(db, set_id)
        except BaseException as e:  # noqa: BLE001 - reported below
            worker_errors.append(e)
        finally:
            worker_done.set()

    real = budget_guard._queued_multishot_estimate
    fired: list[bool] = []

    def _between_reads(*args, **kwargs):
        if not fired:
            fired.append(True)
            threading.Thread(target=_worker, daemon=True).start()
            # Give the worker every chance to commit the fan-out right here.
            worker_done.wait(timeout=3)
        return real(*args, **kwargs)

    monkeypatch.setattr(budget_guard, "_queued_multishot_estimate", _between_reads)
    shot2 = _shot(pid)
    r = _frame(client, shot2, key="ms-race", batch_size=4)  # 6 + 4 >= 9
    assert fired
    assert r.status_code == 403, r.text
    assert worker_done.wait(timeout=30), "worker never finished"
    assert worker_errors == [], worker_errors
    with session_scope() as db:
        assert db.get(MultishotSet, uuid.UUID(set_id)).status != "queued"


# --- review B5-M1: character master / sheet honour the same key -----------------


def _character_client():
    from app.routers import characters as characters_router

    api = FastAPI()
    register_middleware(api)
    api.include_router(characters_router.router, prefix="/api/v1")
    return TestClient(api)


def test_character_master_and_sheet_replay_the_same_key(env):
    from app.models.character import Character

    pid = env["project_id"]
    _budget(pid, None)
    with session_scope() as db:
        char = Character(project_id=pid, name="Hero", appearance_prompt="short black hair")
        db.add(char)
        db.flush()
        cid = char.id
    client = _character_client()

    def _jobs() -> int:
        return _count(Generation, Generation.project_id == pid)

    headers = {"Idempotency-Key": "master-1"}
    r1 = client.post(f"/api/v1/characters/{cid}/master", json={}, headers=headers)
    assert r1.status_code == 202, r1.text
    after_first = _jobs()
    r2 = client.post(f"/api/v1/characters/{cid}/master", json={}, headers=headers)
    assert r2.status_code == 202, r2.text
    assert r2.json() == r1.json()
    assert _jobs() == after_first
    r3 = client.post(
        f"/api/v1/characters/{cid}/master", json={"prompt": "other"}, headers=headers
    )
    assert r3.status_code == 409, r3.text
    assert r3.json()["code"] == "IDEMPOTENCY_CONFLICT"

    body = {"views": ["turnaround_front"], "expressions": [], "outfits": [], "poses": []}
    headers = {"Idempotency-Key": "sheet-1"}
    s1 = client.post(f"/api/v1/characters/{cid}/sheet", json=body, headers=headers)
    assert s1.status_code == 202, s1.text
    assert s1.json()["job_ids"]
    after_sheet = _jobs()
    s2 = client.post(f"/api/v1/characters/{cid}/sheet", json=body, headers=headers)
    assert s2.status_code == 202, s2.text
    assert s2.json() == s1.json()
    assert _jobs() == after_sheet


# --- review B5R2-H1: a set queued last month keeps its period at fan-out --------


def test_last_month_multishot_fan_out_stays_in_its_period(env):
    """P01: a coverage set approved last month is still queued; this month a
    4 x $1 frame request on a $9 budget is accepted (the set is not in this
    month). Fanning the set out must not move its $6 into this month."""
    from app.models.multishot import COVERAGE_ANGLES, MultishotFrame, MultishotSet
    from app.services import multishot as multishot_service

    pid, client = env["project_id"], env["client"]
    _price(Decimal("1"))
    _budget(pid, 9)
    base = _shot(pid)
    with session_scope() as db:
        frame = Generation(
            project_id=pid, shot_id=base, job_type="text_to_image", model_id=IMAGE_MODEL,
            status="succeeded", output_path="x.png", actual_cost_usd=Decimal(0),
            created_at=_last_month(), reserved_at=_last_month(),
        )
        db.add(frame)
        db.flush()
        db.get(Shot, base).approved_frame_generation_id = frame.id
        mset = MultishotSet(
            shot_id=base, source_frame_generation_id=frame.id,
            state_snapshot={"model_id": IMAGE_MODEL}, status="queued",
            created_at=_last_month(),
        )
        db.add(mset)
        db.flush()
        for angle in COVERAGE_ANGLES:
            db.add(MultishotFrame(multishot_set_id=mset.id, coverage_type=angle, status="pending"))
        set_id = str(mset.id)
    assert _committed(pid) == (Decimal(0), Decimal(0))

    r = _frame(client, _shot(pid), key="this-month", batch_size=4)
    assert r.status_code == 202, r.text
    assert _committed(pid) == (Decimal(0), Decimal("4"))

    with session_scope() as db:
        multishot_service.run_multishot(db, set_id)
    start, _end = budget_guard._month_bounds_utc()
    with session_scope() as db:
        assert db.get(MultishotSet, uuid.UUID(set_id)).status != "queued"
        leaves = (
            db.query(Generation)
            .filter(Generation.project_id == pid, Generation.status == "queued",
                    Generation.reserved_at < start)
            .count()
        )
    assert leaves == len(COVERAGE_ANGLES)  # the 6 leaves stay in last month
    # The fan-out kept last month's period: this month is still 4, not 10.
    assert _committed(pid) == (Decimal(0), Decimal("4"))
