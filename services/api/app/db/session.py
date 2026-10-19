"""Engine and session factory. DSN comes from settings only (no hardcoded URL)."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base  # noqa: F401  (re-exported for alembic env)

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)
    return _SessionLocal


def reset_engine() -> None:
    """Tests only."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Worker / script usage."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- after-commit dispatch -------------------------------------------------
# Jobs must reach the queue only AFTER their generation row is committed;
# otherwise a fast worker reads a row that does not exist yet. Services
# register dispatch callbacks; they run on successful commit, and are dropped
# on rollback.
_PENDING_DISPATCH: dict[int, list[Callable[[], None]]] = {}


def dispatch_after_commit(db: Session, fn: Callable[[], None]) -> None:
    """Run fn after the session's next successful commit (dropped on rollback)."""
    _PENDING_DISPATCH.setdefault(id(db), []).append(fn)


def _run_pending(session: Session) -> None:
    for fn in _PENDING_DISPATCH.pop(id(session), []):
        fn()


def _drop_pending(session: Session) -> None:
    _PENDING_DISPATCH.pop(id(session), None)


event.listen(Session, "after_commit", _run_pending)
event.listen(Session, "after_rollback", _drop_pending)
