"""In-process pub/sub for SSE events.

The runner publishes ``EventEnvelope`` objects to ``bus.publish(run_id, env)``;
each connected SSE client holds an ``asyncio.Queue`` returned by
``bus.subscribe(run_id)``. Persistence is the SQLite store's job — the bus only
fans out *live* events to subscribers that are connected right now.

When a client connects mid-run, the SSE handler:
  1. Replays persisted events from SQLite (everything up to ``latest_seq``).
  2. Subscribes to the bus for events with seq strictly greater than that.
  3. The handler de-duplicates by ``seq`` if a race produces overlap.

Why not Redis Pub/Sub: single-Docker constraint, single-process worker model.
``asyncio.Queue`` per subscriber is sufficient and avoids a Redis dep.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import AsyncIterator, Dict, List, Optional, Set

from webapp.schemas import EventEnvelope


logger = logging.getLogger(__name__)


class EventBus:
    """Per-run-id fan-out of EventEnvelope to async subscribers.

    Thread-safety: ``publish`` may be called from the worker thread; it forwards
    each subscriber's ``Queue.put_nowait`` via ``loop.call_soon_threadsafe``.
    Subscribers always live in the FastAPI event loop.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        # Captured at construction time; the FastAPI lifespan ensures one loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the bus to the FastAPI loop. Called once from the lifespan."""
        self._loop = loop

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        """Return a fresh queue that receives every future event for ``run_id``."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subs[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subs[run_id].discard(queue)
            if not self._subs[run_id]:
                del self._subs[run_id]

    def publish(self, run_id: str, env: EventEnvelope) -> None:
        """Thread-safe fan-out. Drops events on subscribers whose queue is full."""
        if self._loop is None:
            # Bus not yet attached (e.g. tests). Silently no-op for the live tail —
            # SQLite still has the event for replay.
            return
        loop = self._loop

        def _enqueue() -> None:
            for queue in list(self._subs.get(run_id, ())):
                try:
                    queue.put_nowait(env)
                except asyncio.QueueFull:
                    logger.warning(
                        "Dropping live SSE event seq=%d for run %s — subscriber queue full",
                        env.seq, run_id,
                    )

        loop.call_soon_threadsafe(_enqueue)

    @contextlib.asynccontextmanager
    async def stream(self, run_id: str) -> AsyncIterator[asyncio.Queue]:
        """Async context manager: subscribe on enter, unsubscribe on exit."""
        queue = await self.subscribe(run_id)
        try:
            yield queue
        finally:
            await self.unsubscribe(run_id, queue)


# Module-level singleton, like the JobStore.
_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
