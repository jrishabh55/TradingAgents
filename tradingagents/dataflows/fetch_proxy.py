"""Optional residential fetch proxy for social-data fetchers.

Reddit and StockTwits rate-limit or block datacenter IPs (per-IP 429s, WAF
403s), which is where hosted deployments live. When the hosting layer can
route a fetch through the requesting user's own machine — e.g. the Drishti
helper's relay websocket — it registers a *resolver* here, and fetchers call
:func:`urlopen_maybe_proxied` instead of ``urlopen`` to transparently gain a
residential egress IP whenever one is available.

With no resolver registered (CLI runs, tests, plain deployments) the wrapper
is a pass-through to ``urlopen`` — behavior is byte-identical to before.

The proxy endpoint contract (implemented by the web layer's relay shim):

    POST {proxy.url}
    Authorization: Bearer {proxy.token}
    {"url": ..., "method": "GET", "headers": {...}}

    → 200 {"ok": true, "status": <upstream status>, "body_b64": ...}
      (any upstream status, including 403/429 — a *completed* exchange)
    → 200 {"ok": false, "error": ...}   (proxy could not perform the fetch)
    → non-200                            (shim/relay problem)

A completed exchange is authoritative: its status is surfaced to the caller
as an ``HTTPError`` exactly as a direct fetch would raise it. Everything
else falls back to a direct fetch, so a missing/offline proxy can never make
a fetch WORSE than today.
"""
from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

#: Extra headroom over the caller's timeout: the proxied fetch traverses the
#: relay websocket and the user's home connection.
_RELAY_OVERHEAD_S = 20.0


@dataclass(frozen=True)
class FetchProxy:
    """Where to send proxied fetches, and the bearer that authorizes them."""

    url: str
    token: str


_resolver: Optional[Callable[[], Optional[FetchProxy]]] = None


def set_resolver(resolver: Optional[Callable[[], Optional[FetchProxy]]]) -> None:
    """Register how to find the current request's proxy (or None to clear).

    The hosting layer decides scope — e.g. a thread-local bound to the run
    being executed, so concurrent runs from different users each proxy
    through their own helper.
    """
    global _resolver
    _resolver = resolver


def _current_proxy() -> Optional[FetchProxy]:
    if _resolver is None:
        return None
    try:
        return _resolver()
    except Exception:  # noqa: BLE001 — a broken resolver must not break fetches
        logger.exception("fetch proxy resolver raised; fetching directly")
        return None


class _ProxiedResponse(io.BytesIO):
    """The slice of ``HTTPResponse`` the fetchers use: context manager,
    ``read()``, and ``status``."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> "_ProxiedResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        self.close()
        return False


def urlopen_maybe_proxied(req: Request, timeout: float, *, direct=None):
    """``urlopen`` that rides the registered fetch proxy when one is active.

    ``direct`` is the plain opener used when no proxy applies (defaults to
    ``urlopen``); callers pass their module's ``urlopen`` so test patches on
    that name keep working.
    """
    opener = direct or urlopen
    proxy = _current_proxy()
    if proxy is None:
        return opener(req, timeout=timeout)

    envelope = json.dumps({
        "url": req.full_url,
        "method": req.get_method(),
        "headers": dict(req.header_items()),
    }).encode("utf-8")
    shim = Request(
        proxy.url,
        data=envelope,
        headers={
            "Authorization": f"Bearer {proxy.token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(shim, timeout=timeout + _RELAY_OVERHEAD_S) as resp:
            payload = json.loads(resp.read())
        if not (isinstance(payload, dict) and payload.get("ok") is True):
            raise ValueError(str((payload or {}).get("error") or "proxy fetch failed"))
        status = int(payload["status"])
        body = base64.b64decode(payload.get("body_b64") or "")
    except Exception as exc:  # noqa: BLE001 — any proxy problem → direct fetch
        logger.debug("fetch proxy unavailable (%s); fetching %s directly",
                     exc, req.full_url)
        return opener(req, timeout=timeout)

    if not 200 <= status < 300:
        # The proxied fetch COMPLETED and this is the upstream's real answer —
        # surface it exactly as a direct fetch would, so callers' existing
        # HTTPError handling (429 backoff, logging) behaves identically.
        raise HTTPError(req.full_url, status, "proxied fetch answered non-2xx",
                        None, io.BytesIO(body))
    return _ProxiedResponse(status, body)
