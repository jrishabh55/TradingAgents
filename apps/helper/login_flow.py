"""In-process ChatGPT sign-in, driven from the helper's local UI.

The CLI ``login`` subcommand blocks a terminal; the desktop app can't. This
runs the same OAuth dance — loopback callback server, PKCE, code exchange —
as a background task the UI starts with a button and observes by polling
``status``. One sign-in at a time; the callback state binds the response to
the attempt that started it.
"""
from __future__ import annotations

import asyncio
import http.server
import secrets
import urllib.parse
from typing import Optional

from apps.helper.credentials.oauth import (
    CALLBACK_PATH,
    DEFAULT_CALLBACK_PORT,
    TokenStore,
    authorize_url,
    callback_port_available,
    exchange_code,
    pkce_pair,
)


class LoginFlow:
    """State machine: idle → pending → done | error (then restartable)."""

    def __init__(self, store: Optional[TokenStore] = None) -> None:
        self._store = store or TokenStore()
        self.status = "idle"
        self.error = ""
        self._task: Optional[asyncio.Task] = None

    def start(self, port: int = DEFAULT_CALLBACK_PORT, timeout_s: float = 300.0) -> str:
        """Begin a sign-in; returns the URL the user's browser must open."""
        if self.status == "pending":
            raise RuntimeError("a sign-in is already in progress — finish it in the browser")
        if not callback_port_available(port):
            # U1: OpenAI may only accept the Codex CLI's registered port, so a
            # collision is reported plainly rather than silently re-porting.
            raise RuntimeError(
                f"port {port} is busy — close any running `codex login` and retry"
            )

        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        url = authorize_url(port=port, challenge=challenge, state=state)
        result: dict = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            # Closure-scoped result dict: no class-level state to bleed
            # between attempts (unlike the CLI's single-shot handler).
            def do_GET(self) -> None:  # noqa: N802 — http.server API
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update(
                    {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                )
                body = b"<html><body><h3>Signed in. You can close this tab.</h3></body></html>"
                if "error" in result:
                    body = b"<html><body><h3>Sign-in failed. Return to the helper app.</h3></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        self.status, self.error = "pending", ""
        self._task = asyncio.create_task(
            self._finish(server, result, verifier, state, port, timeout_s)
        )
        return url

    async def _finish(
        self,
        server: http.server.HTTPServer,
        result: dict,
        verifier: str,
        state: str,
        port: int,
        timeout_s: float,
    ) -> None:
        try:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(server.handle_request), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"no sign-in completed within {timeout_s:.0f}s")
            finally:
                server.server_close()

            if not result:
                raise RuntimeError("the browser never called back — try again")
            if "error" in result:
                detail = result.get("error_description") or result["error"]
                raise RuntimeError(str(detail))
            if result.get("state") != state:
                # Not from the attempt we started; treat as hostile, not odd.
                raise RuntimeError("state mismatch — sign-in response rejected")

            tokens = await exchange_code(result["code"], verifier, port=port)
            self._store.save(tokens)
            self.status = "done"
        except Exception as exc:  # noqa: BLE001 — the UI needs the reason, not a crash
            self.status = "error"
            self.error = str(exc)
