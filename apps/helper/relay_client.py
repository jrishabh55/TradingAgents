"""Helper side of the relay: dial out, execute locally, answer.

The helper connects to the hosted server rather than the other way round, which
is what makes this work behind NAT with no port forwarding, no tunnel, and
nothing about the user's machine publicly reachable.

Calls are executed concurrently as tasks. Handling them inline would serialise a
run behind its slowest call and, worse, block the socket so a ``cancel`` frame
could not be read until the very call it wants to cancel had finished.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Dict, Optional

from apps.helper.credentials import CredentialError
from apps.helper.inbound import parse_chat_completions
from apps.helper.outbound import render_chat_completion
from apps.helper.registry import CallContext, Registry, default_registry
from apps.helper.types import HelperError

logger = logging.getLogger("ta-helper.relay")

#: Reconnect backoff. Capped so a laptop that slept for hours still comes back
#: promptly rather than waiting out an ever-doubling delay.
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0


class RelayClient:
    """Maintains one outbound connection and serves calls from it."""

    def __init__(
        self,
        url: str,
        auth_token: str,
        *,
        registry: Optional[Registry] = None,
        call_timeout_s: float = 600.0,
    ) -> None:
        self.url = url
        self._auth = auth_token
        self._registry = registry or default_registry()
        self._call_timeout_s = call_timeout_s
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Connect, serve, reconnect. Returns only when :meth:`stop` is called."""
        backoff = _BACKOFF_START_S
        while not self._stopping.is_set():
            try:
                await self._session()
                backoff = _BACKOFF_START_S  # a clean session resets the delay
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure is retryable
                logger.warning("relay session ended (%s); retrying in %.0fs", exc, backoff)
            if self._stopping.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def stop(self) -> None:
        self._stopping.set()

    async def _session(self) -> None:
        import websockets

        headers = {"Authorization": f"Bearer {self._auth}"}
        async with websockets.connect(self.url, additional_headers=headers) as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "providers": self._registry.names(),
            }))
            logger.info("relay connected to %s", self.url)
            async for raw in ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "call":
                    # A task, not inline: inline handling would block the socket
                    # so a `cancel` frame could not be read until the call it
                    # cancels had already finished.
                    call_id = message.get("id", "")
                    self._tasks[call_id] = asyncio.create_task(
                        self._handle_call(ws, message)
                    )
                elif kind == "cancel":
                    task = self._tasks.pop(message.get("id", ""), None)
                    if task is not None:
                        task.cancel()
                elif kind == "error":
                    logger.error("relay rejected the connection: %s", message.get("error"))
                    return

    async def _handle_call(self, ws: Any, message: Dict[str, Any]) -> None:
        call_id = message.get("id", "")
        provider_name = message.get("provider", "")
        try:
            payload = await self._execute(provider_name, message.get("body") or {})
        except asyncio.CancelledError:
            # The server gave up on this call; say nothing and let it go.
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("relay call failed")
            payload = {
                "status": 500,
                "body": {"error": {"message": f"{type(exc).__name__}: {exc}",
                                   "type": "helper_error"}},
            }
        finally:
            self._tasks.pop(call_id, None)

        with contextlib.suppress(Exception):
            await ws.send(json.dumps({"type": "result", "id": call_id, "payload": payload}))

    async def _execute(self, provider_name: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Run one Chat Completions request through the local adapter."""
        import time
        import uuid

        provider = self._registry.get(provider_name)
        if provider is None:
            return _error(400, f"unknown provider {provider_name!r}", "invalid_request_error")

        try:
            req = parse_chat_completions(body)
        except HelperError as exc:
            return {"status": exc.status, "body": exc.envelope()}

        if req.stream:
            return _error(501, "streaming is not supported over the relay",
                          "streaming_unsupported")

        try:
            cred = await provider.credential()
        except CredentialError as exc:
            # Surfaced verbatim so the hosted UI can tell the user to run
            # `codex login` on their own machine — the server cannot fix this.
            return _error(401, f"{exc.message}. {exc.remedy}".strip(), "authentication_error")

        ctx = CallContext(timeout_s=self._call_timeout_s)
        try:
            resp = await provider.adapter.send(req, cred, provider.quirks, ctx)
        except HelperError as exc:
            return {"status": exc.status, "body": exc.envelope()}
        except ValueError as exc:  # model resolution lists valid names
            return _error(400, str(exc), "invalid_request_error")

        return {
            "status": 200,
            "body": render_chat_completion(
                resp,
                response_id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
                created=int(time.time()),
            ),
        }


def _error(status: int, message: str, kind: str) -> Dict[str, Any]:
    return {"status": status, "body": {"error": {"message": message, "type": kind}}}
