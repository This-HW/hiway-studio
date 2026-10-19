"""Redis Pub/Sub -> SSE bridge (design report §8.2).

Workers publish here; the `GET /api/v1/events` router streams from here.
Two backends:
  - "redis"  (default): production. Publish is synchronous so Celery tasks can
               call it without an event loop.
  - "memory": in-process fan-out for tests and `make demo`-style runs.

Event name vocabulary is canonical in packages/contracts/sse_events.json.
Envelope on the wire: {"id": int, "event": str, "data": dict}.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
from typing import Any, AsyncIterator, Optional

from app.core.config import get_settings

CHANNEL = "job_events"

_id_counter = itertools.count(1)


class _MemoryBackend:
    """Thread-safe in-process pub/sub. Subscribers poll via asyncio.to_thread so a
    sync Celery worker thread can publish while the API event loop consumes."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def publish(self, channel: str, envelope: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(envelope)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        try:
            while True:
                try:
                    item = await asyncio.to_thread(q.get, True, 1.0)
                except queue.Empty:
                    yield {"heartbeat": True}
                    continue
                yield item
        finally:
            with self._lock:
                if q in self._subs:
                    self._subs.remove(q)


class _RedisBackend:
    def __init__(self, url: str) -> None:
        import redis as redis_sync

        self._sync = redis_sync.Redis.from_url(url, decode_responses=True)

    def publish(self, channel: str, envelope: dict[str, Any]) -> None:
        self._sync.publish(channel, json.dumps(envelope, ensure_ascii=False, default=str))

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        import redis.asyncio as redis_async

        client = redis_async.Redis.from_url(get_settings().redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    yield {"heartbeat": True}
                    continue
                yield json.loads(msg["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await client.aclose()


class SseBridge:
    def __init__(self, backend: str, redis_url: str = "") -> None:
        self._impl = _MemoryBackend() if backend == "memory" else _RedisBackend(redis_url)

    def publish(self, channel: str, event: str, data: dict[str, Any]) -> None:
        """Synchronous on purpose: callable from Celery tasks."""
        self._impl.publish(channel, {"id": next(_id_counter), "event": event, "data": data})

    def subscribe(self, channel: str = CHANNEL) -> AsyncIterator[dict[str, Any]]:
        return self._impl.subscribe(channel)


_bridge: Optional[SseBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> SseBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            settings = get_settings()
            _bridge = SseBridge(settings.sse_backend, settings.redis_url)
        return _bridge


def reset_bridge() -> None:
    """Tests only: drop the cached bridge so a new backend can be selected."""
    global _bridge
    with _bridge_lock:
        _bridge = None
