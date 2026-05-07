"""GET /api/runs/{run_id}/events — Server-Sent Events stream.

Protocol:
1. The handler reads the optional ``Last-Event-ID`` request header (browsers
   send it automatically on EventSource auto-reconnect).
2. Replays every persisted event with ``seq > last_event_id`` from SQLite.
3. Subscribes to the in-process event bus for live tail.
4. Emits ``heartbeat`` every 15 s so reverse proxies (nginx, Cloudflare) don't
   tear down idle connections.
5. Closes when the run reaches a terminal status AND the live subscriber queue
   has drained.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from apps.api.auth import current_user_id
from apps.api.jobs.bus import get_bus
from apps.api.jobs.store import get_store
from apps.api.schemas import EventEnvelope


router = APIRouter()
logger = logging.getLogger(__name__)


HEARTBEAT_SECONDS = 15
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> EventSourceResponse:
    store = get_store()
    detail = store.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Ownership check matches routes/runs.py:get_run. 404 (not 403) so the
    # response doesn't confirm a run by this id exists for someone else.
    owner = detail.user_id or "anonymous"
    if owner != user_id:
        raise HTTPException(status_code=404, detail="run not found")

    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def gen() -> AsyncIterator[dict]:
        # 1. Replay everything persisted past last_event_id.
        replayed_seq = 0
        for env in store.replay_events(run_id, since_seq=last_event_id):
            yield _format(env)
            replayed_seq = env.seq

        # If terminal at end of replay AND nothing new is coming, finish here.
        detail_after_replay = store.get_run(run_id)
        if detail_after_replay and detail_after_replay.status in TERMINAL_STATUSES:
            return

        # 2. Subscribe for live tail.
        bus = get_bus()
        async with bus.stream(run_id) as queue:
            # 3. Loop with a heartbeat timeout so we send keepalives even when
            #    no events are flowing.
            while True:
                if await request.is_disconnected():
                    return

                try:
                    env: EventEnvelope = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    # Re-check terminal status; the worker may have finished while idle.
                    detail_now = store.get_run(run_id)
                    if detail_now and detail_now.status in TERMINAL_STATUSES:
                        return
                    continue

                # De-dup: a fast worker may have appended + published before our
                # replay caught up; skip anything we've already shipped.
                if env.seq <= replayed_seq:
                    continue
                replayed_seq = env.seq
                yield _format(env)

                if env.type in {"run.final", "run.failed", "run.cancelled"}:
                    return

    return EventSourceResponse(gen(), ping=None)  # we send our own heartbeats


def _format(env: EventEnvelope) -> dict:
    """sse-starlette's expected dict shape: {event, id, data}."""
    return {
        "event": env.type,
        "id": str(env.seq),
        "data": json.dumps(
            {"seq": env.seq, "ts": env.ts.isoformat(), "type": env.type, "data": env.data},
            default=str,
        ),
    }


def _parse_last_event_id(raw: Optional[str]) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
