"""QC report total order (B4R3-H1, ADR-0013 B4-r3).

The "latest" QC report of a timeline gates the render (ADR-0006). It used to
be chosen by `created_at DESC` alone: two reports with the same timestamp
(SQLite CURRENT_TIMESTAMP has 1 s resolution; PG `now()` is the transaction
start) had no defined order, so an old `pass` could hide a newer `fail` /
`pending`, and an old v1 `warn` could turn a new v2 `fail` into `stale`.
Reports now carry `seq`, a per-timeline issue number assigned under the
timeline row lock (UNIQUE(target_type, target_id, seq)); every latest-report
query orders by it.

Runs on a per-test SQLite file by default; with TEST_DATABASE_URL pointing at
a dedicated *test Postgres database the same tests run there (schema reset
per test, guarded by conftest.reset_test_postgres).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
import app.main  # noqa: F401  (imports every model module for create_all)
from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.models.project import Project
from app.models.qc import QCReport
from app.models.timeline import Timeline
from app.services import qc_orchestrator
from tests.conftest import isolated_engine, postgres_tests_enabled

_TIE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url, engine = isolated_engine(tmp_path, "qc_order.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    sent: list[tuple] = []
    monkeypatch.setattr(qc_orchestrator, "dispatch_job", lambda *a: sent.append(a))
    with session_scope() as db:
        project = Project(title="qc-order")
        db.add(project)
        db.flush()
        timeline = Timeline(project_id=project.id, name="T", content_version=1)
        db.add(timeline)
        db.flush()
        ids = {"project_id": project.id, "timeline_id": timeline.id}
    yield {**ids, "sent": sent}
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()
    engine.dispose()


def _request(tid) -> uuid.UUID:
    """request_qc in its own committed transaction (the API path)."""
    with session_scope() as db:
        return uuid.UUID(qc_orchestrator.request_qc(db, tid)["qc_report_id"])


def _finish(rid, overall, *, created_at=_TIE) -> None:
    """Complete a report (as the worker would) and pin its timestamp."""
    with session_scope() as db:
        db.execute(
            update(QCReport)
            .where(QCReport.id == rid)
            .values(overall=overall, created_at=created_at)
        )


def _bump(tid, version: int) -> None:
    with session_scope() as db:
        db.execute(
            update(Timeline).where(Timeline.id == tid).values(content_version=version)
        )


def _latest(tid):
    with session_scope() as db:
        r = qc_orchestrator.latest_report(db, tid)
        return (r.id, r.overall) if r is not None else None


def _gate(tid, *, ignore_warnings=False) -> str | None:
    """None when the render gate allows; else details.reason."""
    with session_scope() as db:
        timeline = db.get(Timeline, tid)
        try:
            qc_orchestrator.assert_render_allowed(
                db, timeline, ignore_warnings=ignore_warnings
            )
        except StudioError as exc:
            assert exc.code == ErrorCode.QC_FAILED
            return exc.details["reason"]
    return None


def _router_latest(tid):
    from app.routers import qc as qc_router

    with session_scope() as db:
        out = qc_router.latest_report(tid, db)
        return (out.id, out.overall) if out is not None else None


# --- reverse tests (fail on the created_at-only selection) -------------------


def test_same_created_at_pass_then_fail_latest_is_fail_and_render_refused(env):
    tid = env["timeline_id"]
    old = _request(tid)
    _finish(old, "pass")
    new = _request(tid)
    _finish(new, "fail")  # same created_at as the old pass

    assert _latest(tid) == (new, "fail")
    assert _router_latest(tid) == (new, "fail")
    assert _gate(tid, ignore_warnings=True) == "failed"


def test_same_created_at_pass_then_pending_render_refused_as_running(env):
    tid = env["timeline_id"]
    old = _request(tid)
    _finish(old, "pass")
    new = _request(tid)  # still pending (worker not run)
    with session_scope() as db:
        db.execute(update(QCReport).where(QCReport.id == new).values(created_at=_TIE))

    assert _latest(tid) == (new, "pending")
    assert _gate(tid, ignore_warnings=True) == "qc_running"


def test_old_warn_v1_then_new_fail_v2_same_created_at_reason_failed_not_stale(env):
    tid = env["timeline_id"]
    old = _request(tid)  # runs against content_version 1
    _finish(old, "warn")
    _bump(tid, 2)
    new = _request(tid)  # runs against content_version 2
    _finish(new, "fail")

    assert _latest(tid) == (new, "fail")
    assert _gate(tid, ignore_warnings=True) == "failed"


def test_server_default_timestamps_never_let_an_old_pass_bypass_a_new_pending(env):
    """No pinned timestamps: SQLite CURRENT_TIMESTAMP is 1 s resolution, so
    two quick inserts usually tie (B4R3-P05). The newer pending must win."""
    tid = env["timeline_id"]
    old = _request(tid)
    with session_scope() as db:
        db.execute(update(QCReport).where(QCReport.id == old).values(overall="pass"))
    new = _request(tid)

    assert _latest(tid) == (new, "pending")
    assert _gate(tid, ignore_warnings=True) == "qc_running"


def test_newest_of_many_same_timestamp_reports_wins(env):
    tid = env["timeline_id"]
    rids = []
    for overall in ("fail", "pass", "warn", "pass", "fail", "pass"):
        rid = _request(tid)
        _finish(rid, overall)
        rids.append(rid)
    assert _latest(tid) == (rids[-1], "pass")
    assert _gate(tid) is None


# --- the sequence itself ----------------------------------------------------------


def test_seq_is_per_timeline_and_increases_in_issue_order(env):
    tid = env["timeline_id"]
    with session_scope() as db:
        other = Timeline(project_id=env["project_id"], name="U")
        db.add(other)
        db.flush()
        other_id = other.id
    a1, b1, a2, b2 = (
        _request(tid),
        _request(other_id),
        _request(tid),
        _request(other_id),
    )
    with session_scope() as db:
        seqs = {r.id: r.seq for r in db.query(QCReport).all()}
    assert (seqs[a1], seqs[a2]) == (1, 2)
    assert (seqs[b1], seqs[b2]) == (1, 2)


def test_direct_orm_inserts_get_seq_even_in_one_flush(env):
    """Rows created without request_qc (tests, tools) still get a distinct
    seq — also several in one flush (one executemany)."""
    tid, pid = env["timeline_id"], env["project_id"]
    with session_scope() as db:
        rows = [
            QCReport(
                project_id=pid,
                target_type="timeline",
                target_id=tid,
                overall=o,
                checks=[],
                timeline_content_version=1,
            )
            for o in ("pass", "warn", "fail")
        ]
        db.add_all(rows)
        db.flush()
        assert sorted(r.seq for r in rows) == [1, 2, 3]
    with session_scope() as db:
        db.add(
            QCReport(
                project_id=pid,
                target_type="timeline",
                target_id=tid,
                overall="pass",
                checks=[],
                timeline_content_version=1,
            )
        )
    with session_scope() as db:
        latest = qc_orchestrator.latest_report(db, tid)
        assert (latest.seq, latest.overall) == (4, "pass")


def test_duplicate_seq_is_rejected_by_the_database(env):
    from sqlalchemy.exc import IntegrityError

    tid, pid = env["timeline_id"], env["project_id"]
    _request(tid)
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(
                QCReport(
                    project_id=pid,
                    target_type="timeline",
                    target_id=tid,
                    overall="pass",
                    checks=[],
                    timeline_content_version=1,
                    seq=1,
                )
            )
            db.flush()


@pytest.mark.skipif(
    not postgres_tests_enabled(),
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)
def test_pg_concurrent_requests_serialize_on_the_timeline_lock(env):
    """Two request_qc transactions overlap: the second waits on the timeline
    row lock, then takes the next seq (no duplicate, no IntegrityError)."""
    tid = env["timeline_id"]
    first_in, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []
    ids: dict[str, uuid.UUID] = {}

    def _first():
        try:
            with session_scope() as db:
                ids["a"] = uuid.UUID(
                    qc_orchestrator.request_qc(db, tid)["qc_report_id"]
                )
                first_in.set()
                release.wait(timeout=30)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            first_in.set()

    def _second():
        try:
            ids["b"] = _request(tid)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    t1 = threading.Thread(target=_first)
    t1.start()
    assert first_in.wait(timeout=30)
    t2 = threading.Thread(target=_second)
    t2.start()
    t2.join(timeout=1.0)
    assert t2.is_alive()  # blocked on the timeline row lock
    release.set()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors, errors
    with session_scope() as db:
        seqs = {r.id: r.seq for r in db.query(QCReport).all()}
    assert (seqs[ids["a"]], seqs[ids["b"]]) == (1, 2)
    assert _latest(tid) == (ids["b"], "pending")


# --- B4R4-M2: SQLite request_qc serializes the MAX(seq) read --------------------


def test_two_connections_overlapping_max_seq_reads_both_get_a_seq(env):
    """Two request_qc transactions on separate connections. A barrier tries to
    hold both right after their `SELECT max(seq)`: before B4R4-M2 the SQLite
    timeline "lock" was a no-op, both read 0 and one INSERT died on the
    UNIQUE constraint. lock_row now takes the database write lock first, so
    the second transaction's MAX read waits for the first commit (the barrier
    times out instead of lining the reads up) and both get a seq."""
    from sqlalchemy import event

    tid = env["timeline_id"]
    engine = sess_mod.get_engine()
    barrier = threading.Barrier(2)
    max_reads: list[int] = []

    def _hold_after_max(conn, cursor, statement, params, context, executemany):
        if "max(qc_reports.seq)" in statement.lower():
            max_reads.append(1)
            try:
                barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass

    done: list[uuid.UUID] = []
    errors: list[BaseException] = []

    def _req():
        try:
            done.append(_request(tid))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    event.listen(engine, "after_cursor_execute", _hold_after_max)
    try:
        threads = [threading.Thread(target=_req) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        event.remove(engine, "after_cursor_execute", _hold_after_max)
    assert all(not t.is_alive() for t in threads)
    assert not errors, errors
    assert len(max_reads) == 2 and len(done) == 2
    with session_scope() as db:
        seqs = sorted(r.seq for r in db.query(QCReport).all())
    assert seqs == [1, 2]


# --- B4R4-H1: legacy reports whose issue order is unknown never authorize ------
#
# Real migration boundary: build the 0014 schema with alembic, commit legacy
# rows with the SAME created_at (old pass first, with the bigger UUID), then
# upgrade to head. No request_qc runs before the gate is checked.

_API_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]
_OLD_ID = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
_NEW_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _alembic_cfg():
    from alembic.config import Config

    # No ini file: env.py would otherwise re-run logging.fileConfig.
    cfg = Config()
    cfg.set_main_option("script_location", str(_API_DIR / "app/db/alembic"))
    return cfg


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """(engine, upgrade): an isolated DB migrated to 0014 only."""
    from alembic import command

    url, engine = isolated_engine(tmp_path, "qc_legacy.db")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "0014_style_extractions")
    yield engine, cfg
    get_settings.cache_clear()
    engine.dispose()


def _legacy_rows(engine, rows) -> uuid.UUID:
    """Insert a project + timeline and `rows` [(id, overall, created_at)] on the
    0014 schema (reflected tables), one commit per report. Returns timeline id."""
    from sqlalchemy import MetaData, Table

    md = MetaData()
    projects, timelines, reports = (
        Table(n, md, autoload_with=engine) for n in ("projects", "timelines", "qc_reports")
    )
    assert "seq" not in reports.c and "order_ambiguous" not in reports.c

    def _u(value: uuid.UUID):
        return value.hex if engine.dialect.name == "sqlite" else value

    pid, tid = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(projects.insert().values(
            id=_u(pid), title="legacy", aspect_ratio="16:9", default_models={},
            is_archived=False))
        conn.execute(timelines.insert().values(
            id=_u(tid), project_id=_u(pid), name="legacy", subtitle_mode="single",
            status="draft", content_version=1))
    for rid, overall, created_at in rows:
        with engine.begin() as conn:
            conn.execute(reports.insert().values(
                id=_u(rid), project_id=_u(pid), target_type="timeline",
                target_id=_u(tid), overall=overall, checks=[],
                timeline_content_version=1, created_at=created_at))
    return tid


def _gate_on(engine, tid, *, ignore_warnings=True) -> str | None:
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        timeline = db.get(Timeline, tid)
        try:
            qc_orchestrator.assert_render_allowed(
                db, timeline, ignore_warnings=ignore_warnings)
        except StudioError as exc:
            assert exc.code == ErrorCode.QC_FAILED
            return exc.details["reason"]
    return None


@pytest.mark.parametrize("newer", ["fail", "pending"])
def test_migrated_tied_legacy_pass_never_opens_the_render_gate(legacy_db, newer):
    """B4R4-H1 (P01): old pass (bigger UUID) then newer fail/pending, same
    created_at, both committed before 0015. The backfill cannot know which
    came first, so the latest report must not authorize a render."""
    from alembic import command

    engine, cfg = legacy_db
    tid = _legacy_rows(engine, [(_OLD_ID, "pass", _TIE), (_NEW_ID, newer, _TIE)])
    command.upgrade(cfg, "head")

    reason = _gate_on(engine, tid)
    assert reason is not None, "a legacy pass of unknown order opened the gate"
    assert reason in ("stale", "failed", "qc_running")


def test_migrated_untied_legacy_history_keeps_its_order_and_gate(legacy_db):
    """Distinct timestamps are a real order: no ambiguity flag, the newest
    legacy report gates as before (no forced re-QC for ordinary history)."""
    from alembic import command
    from sqlalchemy.orm import Session

    engine, cfg = legacy_db
    t0 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    tid = _legacy_rows(engine, [(_OLD_ID, "fail", t0), (_NEW_ID, "pass", _TIE)])
    command.upgrade(cfg, "head")

    with Session(engine) as db:
        rows = {r.id: (r.seq, r.order_ambiguous) for r in db.query(QCReport).all()}
    assert rows == {_OLD_ID: (1, False), _NEW_ID: (2, False)}
    assert _gate_on(engine, tid) is None


def test_migrated_ambiguous_history_is_resolved_by_a_fresh_qc(legacy_db, monkeypatch):
    """After the forced re-QC the new report (never ambiguous) gates normally."""
    from alembic import command
    from sqlalchemy.orm import Session

    engine, cfg = legacy_db
    tid = _legacy_rows(engine, [(_OLD_ID, "pass", _TIE), (_NEW_ID, "fail", _TIE)])
    command.upgrade(cfg, "head")
    with Session(engine) as db:
        flags = {r.id: r.order_ambiguous for r in db.query(QCReport).all()}
    assert flags == {_OLD_ID: True, _NEW_ID: True}
    assert _gate_on(engine, tid) == "stale"

    monkeypatch.setattr(qc_orchestrator, "dispatch_job", lambda *a: None)
    with Session(engine) as db, db.begin():
        rid = uuid.UUID(qc_orchestrator.request_qc(db, tid)["qc_report_id"])
    with Session(engine) as db, db.begin():
        report = db.get(QCReport, rid)
        assert (report.seq, report.order_ambiguous) == (3, False)
        report.overall = "pass"
    assert _gate_on(engine, tid) is None
