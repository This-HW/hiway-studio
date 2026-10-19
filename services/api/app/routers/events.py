"""Server-Sent Events endpoint (design report §8.2).

GET /api/v1/events — text/event-stream. Single global stream (single-user app).
Supports Last-Event-ID on reconnect (best effort: the memory backend cannot
replay missed events; Redis Streams replay arrives in a later phase if needed).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.sse_bridge import CHANNEL, get_bridge

router = APIRouter()


@router.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    bridge = get_bridge()
    last_event_id = request.headers.get("last-event-id")

    async def gen():
        async for msg in bridge.subscribe(CHANNEL):
            if msg.get("heartbeat"):
                yield ": ping\n\n"
                continue
            data_json = json.dumps(msg["data"], ensure_ascii=False, default=str)
            yield f"id: {msg['id']}\nevent: {msg['event']}\ndata: {data_json}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
