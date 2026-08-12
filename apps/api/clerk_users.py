"""Clerk Backend API gate: activation + credits, stored in privateMetadata.

The Clerk dashboard is the admin UI — Users → (user) → Metadata → Private:

    { "activated": true, "credits": 10 }

- ``activated`` — default deny. Only users the admin flips to ``true`` can
  use the API at all (enforced by the auth middleware, apps/api/auth.py).
- ``credits`` — one credit per fresh analysis run (debited in routes/runs.py).
  Seeded to ``DEFAULT_CREDITS`` the first time an activated user is seen
  without the key, so activating a user is a one-checkbox operation.

Requires ``CLERK_SECRET_KEY`` (privateMetadata is only reachable via the
Backend API). When unset the whole gate is disabled — JWKS-only deployments
and local dev keep working; create_app() logs a warning at boot.

Gate reads are cached per user for ``_CACHE_TTL`` seconds, so enforcement
costs ~1 Clerk call per user per minute and dashboard edits propagate within
that window. Everything here is sync (stdlib urllib) — the async middleware
calls it via run_in_threadpool, sync routes call it directly.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CLERK_API_BASE = "https://api.clerk.com/v1"
DEFAULT_CREDITS = 10
_CACHE_TTL = 60.0  # seconds; also the max delay for dashboard edits to apply


@dataclass(frozen=True)
class Gate:
    activated: bool
    credits: int


_cache: dict[str, tuple[Gate, float]] = {}  # user_id -> (gate, expires_at)
_cache_lock = threading.Lock()
_debit_locks: dict[str, threading.Lock] = {}
_debit_locks_guard = threading.Lock()


def enabled() -> bool:
    """Whether the activation/credits gate is on (CLERK_SECRET_KEY set)."""
    return bool(os.environ.get("CLERK_SECRET_KEY", "").strip())


def reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    key = os.environ.get("CLERK_SECRET_KEY", "").strip()
    req = urllib.request.Request(
        f"{CLERK_API_BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _patch_credits(user_id: str, credits: int) -> None:
    # PATCH /users/{id}/metadata merges — other privateMetadata keys
    # (notably ``activated``) survive untouched.
    _request(
        "PATCH",
        f"/users/{user_id}/metadata",
        {"private_metadata": {"credits": credits}},
    )


def _fetch_gate(user_id: str) -> Gate:
    """Read the gate from Clerk, seeding credits for newly activated users."""
    try:
        user = _request("GET", f"/users/{user_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # A sub we can verify but Clerk doesn't know (deleted user, or a
            # token from another Clerk app) is simply not activated.
            return Gate(activated=False, credits=0)
        raise
    meta = user.get("private_metadata") or {}
    activated = meta.get("activated") is True
    credits = meta.get("credits")
    if activated and not isinstance(credits, int):
        credits = DEFAULT_CREDITS
        _patch_credits(user_id, credits)
    return Gate(activated=activated, credits=credits if isinstance(credits, int) else 0)


def _cache_put(user_id: str, gate: Gate) -> None:
    with _cache_lock:
        _cache[user_id] = (gate, time.monotonic() + _CACHE_TTL)


def get_gate(user_id: str) -> Gate:
    """Cached gate lookup. Raises on Clerk API failure with no cached value."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(user_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        gate = _fetch_gate(user_id)
    except Exception:
        if hit is not None:
            # Clerk API hiccup: serve the stale value rather than locking out
            # every active user. The next request retries the fetch.
            logger.warning("Clerk gate fetch failed for %s; using stale cache", user_id)
            return hit[0]
        raise
    _cache_put(user_id, gate)
    return gate


def _debit_lock(user_id: str) -> threading.Lock:
    with _debit_locks_guard:
        return _debit_locks.setdefault(user_id, threading.Lock())


def debit_credit(user_id: str) -> Optional[int]:
    """Spend one credit. Returns the new balance, or None when broke.

    Reads fresh from Clerk (not the cache) so dashboard top-ups count
    immediately, then writes the decremented balance back.
    """
    # ponytail: per-user in-process lock — Clerk's metadata PATCH has no CAS,
    # so this is only race-safe while the API runs as a single process. Move
    # the ledger into SQLite (jobs/store.py) if this ever goes multi-replica.
    with _debit_lock(user_id):
        gate = _fetch_gate(user_id)
        if not gate.activated or gate.credits <= 0:
            _cache_put(user_id, gate)
            return None
        spent = Gate(activated=True, credits=gate.credits - 1)
        _patch_credits(user_id, spent.credits)
        _cache_put(user_id, spent)
        return spent.credits
