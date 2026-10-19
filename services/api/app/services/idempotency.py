"""Idempotency keys for generation-creating endpoints (D-13.2).

The `Idempotency-Key` request header de-duplicates double submissions
(double-clicks, client retries) within a 60-second window ([assumption] #15).

Scope is per-endpoint: the lookup key is (endpoint_path, raw_key), so the
same header value on two different endpoints does not collide.

In-memory TTL store. The check happens in the API process only, and the
deployment is a single local API process, so no shared backend is needed.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.core import defaults


class IdempotencyStore:
    """Thread-safe TTL map: key -> (expires_at, response_body)."""

    def __init__(self, window_sec: int | None = None) -> None:
        self._window = window_sec or defaults.IDEMPOTENCY_WINDOW_SEC
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def make_key(self, endpoint: str, raw_key: str) -> str:
        return f"{endpoint}\x00{raw_key.strip()}"

    def recall(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, body = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            return dict(body)

    def remember(self, key: str, response_body: dict[str, Any]) -> None:
        with self._lock:
            self._evict_expired_locked()
            self._entries[key] = (time.monotonic() + self._window, dict(response_body))

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        stale = [k for k, (exp, _) in self._entries.items() if exp <= now]
        for k in stale:
            del self._entries[k]


_store = IdempotencyStore()


def get_store() -> IdempotencyStore:
    return _store


def recall(route_template: str, raw_key: str):
    """Replay the stored response for (route_template, raw_key), or None.

    Returns a JobAcceptedResponse-compatible object when present; the router
    converts it back to the response model.
    """
    store = get_store()
    body = store.recall(store.make_key(route_template, raw_key))
    if body is None:
        return None
    from app.schemas.generation import JobAcceptedResponse

    return JobAcceptedResponse.model_validate(body)


def remember(route_template: str, raw_key: str, response) -> None:
    """Store a response for (route_template, raw_key) inside the window."""
    store = get_store()
    store.remember(store.make_key(route_template, raw_key), response.model_dump(mode="json"))
