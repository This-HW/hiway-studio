"""Test environment defaults. Set BEFORE any app.* import (pytest imports this first).

Database isolation (R2-C1): the service DATABASE_URL (e.g. from .env inside
`docker compose run api`) is NEVER used by the test suite — it is replaced
here by an in-memory SQLite URL, and every DB fixture uses its own temp
SQLite file. Postgres runs only when TEST_DATABASE_URL is set explicitly,
and the destructive per-test reset (DROP SCHEMA) first verifies on the live
connection that the database is a dedicated test database (name ending in
"test", e.g. ``hiway_test``); anything else fails the test without a single
statement against that database. The other variables keep setdefault.
"""
import os

# The service DB URL is never inherited (R2-C1).
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("PROJECTS_ROOT", "/tmp/ai-studio-test-projects")
os.environ.setdefault("SSE_BACKEND", "memory")


# ---------------------------------------------------------------------------
# External-tool markers.
#
#   @pytest.mark.needs_ffmpeg    ffmpeg + ffprobe on PATH (or FFMPEG_BIN)
#   @pytest.mark.needs_cjk_font  a font from defaults.CJK_FONT_CANDIDATES
#
# Missing tool -> visible skip. With REQUIRE_MEDIA_TOOLS=1 (CI / Docker image)
# a missing tool is a hard failure instead, so nothing is silently skipped.
# ---------------------------------------------------------------------------
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.core import defaults  # noqa: E402


def media_tools_required() -> bool:
    return os.environ.get("REQUIRE_MEDIA_TOOLS", "").strip().lower() in ("1", "true", "yes")


def find_cjk_font() -> str | None:
    return next((p for p in defaults.CJK_FONT_CANDIDATES if Path(p).exists()), None)


def _missing_for(marker: str) -> str | None:
    if marker == "needs_ffmpeg":
        names = [os.environ.get("FFMPEG_BIN") or "ffmpeg", "ffprobe"]
        missing = [n for n in names if shutil.which(n) is None]
        return f"missing: {', '.join(missing)}" if missing else None
    if marker == "needs_cjk_font":
        return None if find_cjk_font() else "no CJK font from defaults.CJK_FONT_CANDIDATES"
    return None


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_ffmpeg: requires ffmpeg and ffprobe")
    config.addinivalue_line("markers", "needs_cjk_font: requires a CJK font")


def pytest_runtest_setup(item):
    for marker in ("needs_ffmpeg", "needs_cjk_font"):
        if item.get_closest_marker(marker) is None:
            continue
        problem = _missing_for(marker)
        if problem is None:
            continue
        if media_tools_required():
            pytest.fail(f"{marker}: {problem} (REQUIRE_MEDIA_TOOLS=1)", pytrace=False)
        pytest.skip(f"{marker}: {problem}")


# ---------------------------------------------------------------------------
# Isolated database (R2-C1).
# ---------------------------------------------------------------------------
TEST_DATABASE_ENV = "TEST_DATABASE_URL"


def configured_test_database_url() -> str:
    """The explicit Postgres test DSN, or "" (then tests use temp SQLite)."""
    return os.environ.get(TEST_DATABASE_ENV, "").strip()


def postgres_tests_enabled() -> bool:
    return configured_test_database_url().startswith("postgresql")


def is_dedicated_test_db_name(name: str | None) -> bool:
    return bool(name) and name.lower().endswith("test")


def reset_test_postgres(url: str):
    """Return an engine on `url` with an empty public schema — ONLY when the
    connected database really is a dedicated test database.

    The URL's database name AND the server's current_database() must end in
    "test"; otherwise pytest.fail without any write (the DROP never runs)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = make_url(url).database
    if not is_dedicated_test_db_name(name):
        pytest.fail(
            f"{TEST_DATABASE_ENV} database {name!r} is not a dedicated test "
            "database (its name must end in 'test', e.g. hiway_test); refusing "
            "to reset it.",
            pytrace=False,
        )
    engine = create_engine(url)
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar_one()
    if not is_dedicated_test_db_name(actual):
        engine.dispose()
        pytest.fail(
            f"{TEST_DATABASE_ENV} connects to database {actual!r}, not a "
            "dedicated test database; refusing to reset it.",
            pytrace=False,
        )
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    return engine


def isolated_engine(tmp_path, filename: str = "test.db"):
    """(url, engine): the Postgres test DB when TEST_DATABASE_URL is set
    (verified + reset), else a fresh SQLite file under tmp_path."""
    from sqlalchemy import create_engine

    url = configured_test_database_url()
    if url.startswith("postgresql"):
        return url, reset_test_postgres(url)
    url = f"sqlite:///{tmp_path}/{filename}"
    return url, create_engine(url)


# ---------------------------------------------------------------------------
# SSE payload contract (B6-M3): every event the code publishes during the
# suite must carry the required fields of packages/contracts/sse_events.json
# with the declared JSON types (optional "name?" fields: type-checked when
# present, None allowed). Extra keys are allowed. Tests that replace
# get_bridge() with their own fake are not covered by this hook.
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402

_SSE_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "sse_events.json"
_SSE_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def sse_contract_violations(event: str, data: object) -> list[str]:
    events = _json.loads(_SSE_CONTRACT_PATH.read_text(encoding="utf-8"))["events"]
    if event not in events:
        return [f"{event}: not declared in sse_events.json"]
    if not isinstance(data, dict):
        return [f"{event}: payload is {type(data).__name__}, not an object"]
    problems: list[str] = []
    for raw_key, type_name in events[event].items():
        optional = raw_key.endswith("?")
        key = raw_key.rstrip("?")
        check = _SSE_TYPES[type_name]
        if key not in data:
            if not optional:
                problems.append(f"{event}: missing required {key!r}")
            continue
        value = data[key]
        if value is None and optional:
            continue
        if not check(value):
            problems.append(f"{event}: {key!r}={value!r} is not a {type_name}")
    return problems


@pytest.fixture(autouse=True)
def _enforce_sse_contract(monkeypatch):
    from app.core import sse_bridge

    violations: list[str] = []
    real_publish = sse_bridge.SseBridge.publish

    def checked_publish(self, channel, event, data):
        violations.extend(sse_contract_violations(event, data))
        return real_publish(self, channel, event, data)

    monkeypatch.setattr(sse_bridge.SseBridge, "publish", checked_publish)
    yield
    if violations:
        pytest.fail("SSE payload contract violations:\n  " + "\n  ".join(sorted(set(violations))),
                    pytrace=False)
