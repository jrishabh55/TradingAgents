"""Reverse-connection relay: hosted server, user's own subscription.

The server cannot reach a laptop behind NAT, so the helper dials OUT and holds a
WebSocket open. The server pushes LLM calls down that socket; the helper executes
them locally against the user's own credential and returns the result. Nothing
about the user's tokens ever reaches the server.

Two properties this module exists to guarantee:

**Per-user routing.** A call is dispatched only to the connection registered for
that ``user_id``. There is no fallback to "any connected helper" — that would
silently spend a stranger's subscription.

**Retries are free.** A dropped socket mid-call is the common failure, and naive
retry re-bills a completed upstream call. Results are cached against a request
fingerprint, and identical in-flight requests share one call rather than racing.
The fingerprint binds the user and provider, so a cached response can never cross
either boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: A helper that has not answered within this window is treated as gone. Runs are
#: minutes long but a SINGLE call is not, so this bounds a hung helper without
#: killing a legitimately slow one.
DEFAULT_CALL_TIMEOUT_S = 900.0

#: How long a completed result stays available for a retry. Long enough to
#: survive a reconnect, short enough not to serve stale analysis.
RESULT_TTL_S = 900.0


class RelayUnavailable(RuntimeError):
    """No helper is connected for this user."""


class RelayTimeout(RuntimeError):
    """The helper accepted the call but never answered."""


def fingerprint(user_id: str, provider: str, body: Dict[str, Any]) -> str:
    """Stable key for a logical LLM call.

    Binds ``user_id`` and ``provider`` deliberately: a bare content hash would
    let one user's cached response satisfy another's identical request, or a
    response from one deployment answer a request meant for another.
    """
    payload = json.dumps(
        {"u": user_id, "p": provider, "b": body}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _Pending:
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class _CachedResult:
    value: Dict[str, Any]
    stored_at: float = field(default_factory=time.monotonic)

    def fresh(self, ttl: float = RESULT_TTL_S) -> bool:
        return (time.monotonic() - self.stored_at) < ttl


class RelayConnection:
    """One connected helper. Owns its in-flight calls."""

    def __init__(self, user_id: str, send_json, *, connection_id: Optional[str] = None) -> None:
        self.user_id = user_id
        self.connection_id = connection_id or uuid.uuid4().hex
        self._send_json = send_json
        self._pending: Dict[str, _Pending] = {}
        self.connected_at = time.time()
        self.providers: list[str] = []

    async def call(
        self, provider: str, body: Dict[str, Any], *, timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    ) -> Dict[str, Any]:
        """Send one call and await its result."""
        call_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[call_id] = _Pending(fut)
        try:
            await self._send_json(
                {"type": "call", "id": call_id, "provider": provider, "body": body}
            )
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            # Tell the helper to stop working; otherwise it keeps streaming and
            # keeps billing against a result nobody will read.
            with contextlib.suppress(Exception):
                await self._send_json({"type": "cancel", "id": call_id})
            raise RelayTimeout(f"helper did not answer within {timeout_s:.0f}s") from exc
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: str, payload: Dict[str, Any]) -> None:
        pending = self._pending.get(call_id)
        if pending and not pending.future.done():
            pending.future.set_result(payload)

    def fail_all(self, reason: str) -> None:
        """Fail every in-flight call — the socket went away."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(RelayUnavailable(reason))
        self._pending.clear()

    @property
    def in_flight(self) -> int:
        return len(self._pending)


class RelayRegistry:
    """user_id -> connection, plus the result cache and single-flight map."""

    def __init__(self, *, result_ttl_s: float = RESULT_TTL_S) -> None:
        self._connections: Dict[str, RelayConnection] = {}
        self._results: Dict[str, _CachedResult] = {}
        self._inflight: Dict[str, asyncio.Future] = {}
        self._result_ttl = result_ttl_s

    # ---- connection lifecycle ----

    def register(self, conn: RelayConnection) -> Optional[RelayConnection]:
        """Attach a helper. Returns the connection it displaced, if any.

        Last writer wins: a user reconnecting (laptop woke, network flapped)
        must not be locked out by a stale registration the server has not yet
        noticed is dead.
        """
        previous = self._connections.get(conn.user_id)
        self._connections[conn.user_id] = conn
        return previous

    def unregister(self, conn: RelayConnection) -> None:
        """Detach, but only if this exact connection is still the registered one."""
        current = self._connections.get(conn.user_id)
        if current is not None and current.connection_id == conn.connection_id:
            del self._connections[conn.user_id]
        conn.fail_all("helper disconnected")

    def get(self, user_id: str) -> Optional[RelayConnection]:
        return self._connections.get(user_id)

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._connections

    def connected_users(self) -> list[str]:
        return sorted(self._connections)

    # ---- dispatch with single-flight + result cache ----

    async def dispatch(
        self,
        user_id: str,
        provider: str,
        body: Dict[str, Any],
        *,
        timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> Dict[str, Any]:
        key = fingerprint(user_id, provider, body)

        cached = self._results.get(key)
        if cached is not None:
            if cached.fresh(self._result_ttl):
                # The retry-after-a-dropped-socket path: already paid for.
                return cached.value
            del self._results[key]

        existing = self._inflight.get(key)
        if existing is not None:
            # An identical call is already upstream. Awaiting it is not just an
            # optimisation — issuing a second would bill twice for one answer.
            return await asyncio.shield(existing)

        conn = self._connections.get(user_id)
        if conn is None:
            raise RelayUnavailable(f"no helper connected for user {user_id!r}")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            result = await conn.call(provider, body, timeout_s=timeout_s)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                # Mark it retrieved. When no other caller is waiting on this
                # single-flight future, an unconsumed exception makes asyncio
                # log a spurious traceback at GC time — noise that would hide
                # real failures in test output and in production logs.
                fut.add_done_callback(
                    lambda f: f.cancelled() or f.exception()
                )
            raise
        else:
            self._results[key] = _CachedResult(result)
            if not fut.done():
                fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)
            self._evict_expired()

    def _evict_expired(self) -> None:
        stale = [k for k, v in self._results.items() if not v.fresh(self._result_ttl)]
        for k in stale:
            del self._results[k]

    # ---- introspection, for tests and /status ----

    def cached_count(self) -> int:
        return len(self._results)


_registry: Optional[RelayRegistry] = None


def get_relay_registry() -> RelayRegistry:
    global _registry
    if _registry is None:
        _registry = RelayRegistry()
    return _registry


def reset_relay_registry_for_tests() -> RelayRegistry:
    global _registry
    _registry = RelayRegistry()
    return _registry
