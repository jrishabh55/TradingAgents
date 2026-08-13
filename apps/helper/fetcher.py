"""Residential fetch executor: the relay's data-proxy calls run here.

The hosted server's datacenter IP gets 403/429'd by social-data sources
(Reddit, StockTwits); this machine's home connection does not. The server
sends ``{"type": "call", "provider": "__fetch__", "body": {url, method,
headers}}`` frames down the relay socket and this module performs the fetch
locally, returning the raw response for the server-side pipeline to parse.

SECURITY — this must never become an open proxy. The allowlist below is
enforced HERE, on the user's machine, precisely so the user is protected
*from the server*: even a fully compromised portal can only make this
machine fetch public data from the pinned hosts. Specifically:

- exact-match host allowlist (no subdomain wildcards, no user additions),
- HTTPS only, GET only, no redirects followed,
- request headers filtered to a benign set (never Authorization/Cookie),
- response size capped.

Widening the allowlist is a deliberate code change shipped in a new helper
build — never server configuration.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import logging
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger("drishti-helper.fetcher")

#: The pseudo-provider name fetch frames arrive under (see relay_client).
FETCH_PROVIDER = "__fetch__"

#: Exact hostnames the helper will fetch from. Public, read-only data
#: sources used by the analysis pipeline — nothing else.
ALLOWED_HOSTS = frozenset({
    "www.reddit.com",
    "reddit.com",
    "oauth.reddit.com",
    "api.stocktwits.com",
})

#: Request headers forwarded to the target. Everything else — notably
#: Authorization and Cookie — is dropped: the proxy carries identity-free
#: public fetches only.
_ALLOWED_HEADERS = frozenset({"user-agent", "accept", "accept-language"})

MAX_BODY_BYTES = 2_000_000
FETCH_TIMEOUT_S = 20.0


class _NoRedirects(HTTPRedirectHandler):
    """Refuse to follow redirects — a 3xx could point off the allowlist."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


def _reject_reason(url: str, method: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "https URLs only"
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return f"host {host!r} is not on the helper's fetch allowlist"
    if method != "GET":
        return "GET requests only"
    return None


async def execute_fetch(body: Dict[str, Any]) -> Dict[str, Any]:
    """Perform one allowlisted fetch off the event loop; never raises."""
    return await asyncio.to_thread(_fetch_blocking, body)


def _fetch_blocking(body: Dict[str, Any]) -> Dict[str, Any]:
    url = str(body.get("url") or "")
    method = str(body.get("method") or "GET").upper()
    reason = _reject_reason(url, method)
    if reason is not None:
        logger.warning("fetch rejected: %s (%s)", reason, url[:120])
        return {"ok": False, "error": reason}

    headers = {
        k: str(v)
        for k, v in (body.get("headers") or {}).items()
        if k.lower() in _ALLOWED_HEADERS
    }
    opener = build_opener(_NoRedirects)
    try:
        with opener.open(Request(url, headers=headers), timeout=FETCH_TIMEOUT_S) as resp:
            data = resp.read(MAX_BODY_BYTES + 1)
            status = resp.status
    except HTTPError as exc:
        # The target ANSWERED (403, 429, …) — that is a completed exchange the
        # server-side caller wants to see verbatim, not a proxy failure.
        try:
            data = exc.read(MAX_BODY_BYTES + 1) or b""
        except Exception:  # noqa: BLE001
            data = b""
        status = exc.code
    except (OSError, http.client.HTTPException) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if len(data) > MAX_BODY_BYTES:
        return {"ok": False, "error": "response exceeds the helper's size cap"}
    return {"ok": True, "status": status,
            "body_b64": base64.b64encode(data).decode("ascii")}
