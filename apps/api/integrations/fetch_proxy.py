"""Per-run wiring for the residential fetch proxy.

The upstream fetchers (reddit.py, stocktwits.py) call
``tradingagents.dataflows.fetch_proxy.urlopen_maybe_proxied``, which consults
a registered resolver. This module provides that resolver as a
**thread-local**: the runner executes each analysis in one worker thread, so
binding the proxy to the thread scopes it to exactly one run — ten
concurrent runs from different users each proxy through their own helper,
and code running outside a run (or a run whose user has no helper online)
resolves to None and fetches directly.

Anything upstream that fetches from its own spawned thread misses the
context and quietly falls back to a direct fetch — safe, just unproxied.
"""
from __future__ import annotations

import threading
from typing import Optional

from tradingagents.dataflows import fetch_proxy as upstream

_local = threading.local()
_installed = False


def install_resolver() -> None:
    """Idempotently point the upstream hook at our thread-local."""
    global _installed
    if not _installed:
        upstream.set_resolver(lambda: getattr(_local, "proxy", None))
        _installed = True


def fetch_shim_url() -> str:
    """Where a worker thread reaches this API's own fetch shim."""
    from apps.api.integrations.helper_backend import DEFAULT_SELF_URL, SELF_URL_ENV
    import os

    base = os.environ.get(SELF_URL_ENV, DEFAULT_SELF_URL).rstrip("/")
    return f"{base}/internal/relay/fetch"


def activate_for_run(user_id: str) -> Optional[str]:
    """Enable proxying in THIS worker thread when the user's helper is online.

    Mints a per-run internal token (same registry the LLM relay uses) and
    returns it so the caller can revoke it when the run ends; returns None —
    and leaves the thread unproxied — when no helper is connected.
    """
    install_resolver()
    from apps.api.integrations.helper_backend import relay_available
    from apps.api.routes.relay import get_internal_tokens

    _local.proxy = None
    if not relay_available(user_id):
        return None
    token = get_internal_tokens().mint(user_id or "anonymous")
    _local.proxy = upstream.FetchProxy(url=fetch_shim_url(), token=token)
    return token


def deactivate(token: Optional[str]) -> None:
    """Clear this thread's proxy and revoke its token. Safe on every path."""
    _local.proxy = None
    if token:
        from apps.api.routes.relay import get_internal_tokens

        get_internal_tokens().revoke(token)
