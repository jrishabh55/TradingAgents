"""Relay transport: the WebSocket helpers dial into, and the loopback shim.

Two routes, deliberately on different path prefixes:

``/api/relay/ws``
    Where a user's helper connects. Public (it must be reachable from their
    laptop) and therefore authenticated per-connection. Starlette's
    ``@app.middleware("http")`` does not run for websocket scope, so this
    endpoint verifies the credential itself rather than assuming the middleware
    did.

``/internal/relay/v1/{provider}/chat/completions``
    What the pipeline's ``backend_url`` points at. NOT under ``/api``, so the
    frontend's ``/api/*`` catch-all proxy does not expose it, and it is
    additionally gated by a per-run internal token. The user id is never in the
    URL — it is resolved from that token — so a public log or referrer cannot
    leak who a run belongs to.

The shim being plain HTTP is the design's main simplification: the pipeline's
worker thread makes an ordinary blocking request, so no cross-thread future
juggling reaches it. Only the async side deals with the socket.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from apps.api.relay import (
    RelayConnection,
    RelayTimeout,
    RelayUnavailable,
    get_relay_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class InternalTokenRegistry:
    """Short-lived tokens binding a shim request to a user.

    Held in memory only. They are minted when a relay-backed run starts and
    dropped when it ends, so a leaked token is useless beyond that run — and no
    user identifier travels in a URL.
    """

    def __init__(self) -> None:
        self._tokens: Dict[str, str] = {}

    def mint(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = user_id
        return token

    def resolve(self, token: str) -> Optional[str]:
        return self._tokens.get(token)

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def __len__(self) -> int:  # for tests
        return len(self._tokens)


_internal_tokens = InternalTokenRegistry()


def get_internal_tokens() -> InternalTokenRegistry:
    return _internal_tokens


async def _authenticate_ws(websocket: WebSocket) -> Optional[str]:
    """Resolve the connecting user, or None to reject.

    Mirrors the HTTP middleware's three modes so the relay behaves the same as
    the rest of the API: Clerk when configured, legacy shared bearer, else open.
    """
    from apps.api.auth import get_verifier
    import os

    header = websocket.headers.get("authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    token = token or (websocket.query_params.get("token") or "").strip()

    # Pairing tokens work in EVERY mode — they are the credential an installed
    # helper daemon holds, since it has no browser session to get a JWT from.
    if token.startswith("tarelay_"):
        from apps.api.jobs.store import get_store

        return get_store().resolve_pair_token(token)

    verifier = get_verifier()
    if verifier is not None:
        if not token:
            return None
        try:
            claims = verifier.verify(token)
        except Exception:  # noqa: BLE001 — any verification failure is a reject
            return None
        subject = claims.get("sub")
        return str(subject) if subject else None

    legacy = os.environ.get("WEBAPP_AUTH_TOKEN", "").strip()
    if legacy:
        return "shared-bearer" if token == legacy else None
    # Open mode: unlike the HTTP routes, this endpoint is NOT safe to leave
    # open. Registration is last-writer-wins per user, so an unauthenticated
    # socket accepting everyone as "anonymous" would let any client displace
    # the real helper and intercept (or forge) that user's LLM traffic. Local
    # dev doesn't need the relay — the loopback helper covers it — so refuse.
    return None


def _public_ws_url(request: Request) -> str:
    """The relay URL as reachable from the USER'S machine.

    ``request.url`` is whatever hop delivered the request — behind the
    frontend's proxy that is an internal hostname (and the proxy does not
    forward websocket upgrades anyway), so prefer explicit deployment config,
    then the standard forwarded headers, and only then the raw request.
    """
    import os
    from urllib.parse import urlsplit

    base = os.environ.get("WEBAPP_PUBLIC_URL", "").strip()
    if base:
        parts = urlsplit(base if "//" in base else f"https://{base}")
        scheme = "ws" if parts.scheme == "http" else "wss"
        return f"{scheme}://{parts.netloc}/api/relay/ws"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    scheme = "wss" if proto == "https" else "ws"
    return f"{scheme}://{host}/api/relay/ws"


@router.post("/api/relay/pair")
def pair_helper(request: Request) -> Dict[str, str]:
    """Mint a pairing token so the user's helper daemon can connect as them.

    Returned exactly once (only its hash is stored). The response includes the
    full ``connect`` command so the UI can offer copy-paste setup.
    """
    import os

    from apps.api.auth import current_user_id, get_verifier
    from apps.api.jobs.store import get_store

    # In open mode every HTTP caller is "anonymous", so minting here would hand
    # any client a token that _authenticate_ws accepts — bypassing the ws
    # endpoint's own refusal of open mode. No auth, no pairing.
    if get_verifier() is None and not os.environ.get("WEBAPP_AUTH_TOKEN", "").strip():
        return JSONResponse(
            status_code=403,
            content={"detail": "helper pairing requires authentication to be "
                               "configured (Clerk or WEBAPP_AUTH_TOKEN)"},
        )

    user_id = current_user_id(request)
    token = get_store().create_pair_token(user_id)
    ws_url = _public_ws_url(request)
    return {
        "token": token,
        "ws_url": ws_url,
        "command": f"python -m apps.helper connect --url {ws_url} --token {token}",
    }


@router.websocket("/api/relay/ws")
async def relay_socket(websocket: WebSocket) -> None:
    user_id = await _authenticate_ws(websocket)
    if user_id is None:
        # 1008 = policy violation. Closing before accept would give the client
        # no way to distinguish "rejected" from "server down".
        await websocket.accept()
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    registry = get_relay_registry()
    conn = RelayConnection(user_id, websocket.send_json)
    displaced = registry.register(conn)
    if displaced is not None:
        displaced.fail_all("replaced by a newer helper connection")
    logger.info("relay connected user=%s conn=%s", user_id, conn.connection_id)

    try:
        # user_id rides along so the helper's UI can show WHOSE runs this
        # connection will serve — a helper paired to the wrong account
        # otherwise just reads "connected" with no way to tell.
        await websocket.send_json(
            {"type": "ready", "connection_id": conn.connection_id, "user_id": user_id}
        )
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "result":
                conn.resolve(message.get("id", ""), message.get("payload") or {})
            elif kind == "hello":
                conn.providers = list(message.get("providers") or [])
                conn.helper_version = str(message.get("version") or "")
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("relay disconnected user=%s", user_id)
    except Exception:  # noqa: BLE001 — a malformed frame must not kill the app
        logger.exception("relay socket error for user=%s", user_id)
    finally:
        registry.unregister(conn)


#: Pseudo-provider for data-proxy frames. Must match apps/helper/fetcher.py.
FETCH_PROVIDER = "__fetch__"

#: A fetch is a single HTTP GET, not a minutes-long LLM call — bound it
#: accordingly so a hung helper fails the fetch (and the caller falls back
#: to a direct fetch) instead of stalling an analyst for 15 minutes.
FETCH_TIMEOUT_S = 60.0


@router.post("/internal/relay/fetch")
async def relay_fetch_shim(request: Request) -> Any:
    """Bridge one pipeline data-fetch onto the user's helper socket.

    Same auth as the LLM shim (per-run internal token). The helper enforces
    its own host allowlist; this route knowingly forwards whatever the
    pipeline asked for and returns the helper's verdict verbatim — including
    ``{"ok": false}`` rejections, which callers treat as "proxy unavailable"
    and satisfy with a direct fetch.
    """
    auth = request.headers.get("authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    user_id = get_internal_tokens().resolve(token) if token else None
    if user_id is None:
        return JSONResponse(status_code=401, content={"ok": False,
                                                      "error": "invalid internal relay token"})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False,
                                                      "error": "request body is not valid JSON"})

    try:
        payload = await get_relay_registry().dispatch(
            user_id, FETCH_PROVIDER, body, timeout_s=FETCH_TIMEOUT_S
        )
    except RelayUnavailable as exc:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})
    except RelayTimeout as exc:
        return JSONResponse(status_code=504, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=200, content=payload)


@router.post("/internal/relay/v1/{provider}/chat/completions")
async def relay_shim(provider: str, request: Request) -> Any:
    """Bridge one blocking pipeline request onto the user's helper socket."""
    auth = request.headers.get("authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    user_id = get_internal_tokens().resolve(token) if token else None
    if user_id is None:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "invalid internal relay token",
                               "type": "authentication_error"}},
        )

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "request body is not valid JSON",
                               "type": "invalid_request_error"}},
        )

    try:
        payload = await get_relay_registry().dispatch(user_id, provider, body)
    except RelayUnavailable as exc:
        # 503 rather than 500: the run can succeed once the helper reconnects,
        # which is materially different from a broken request.
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "helper_disconnected"}},
        )
    except RelayTimeout as exc:
        return JSONResponse(
            status_code=504,
            content={"error": {"message": str(exc), "type": "helper_timeout"}},
        )

    status = int(payload.get("status") or 200)
    return JSONResponse(status_code=status, content=payload.get("body") or {})
