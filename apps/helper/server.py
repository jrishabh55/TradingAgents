"""The loopback HTTP server.

Exposes ``POST /v1/{provider}/chat/completions``. Provider selection rides the
URL path so one helper instance serves several subscriptions at once and the
pipeline picks one by pointing its existing per-run ``backend_url`` at a
different path — verified against ``openai`` 2.33.0, which appends rather than
``urljoin``-resolves, so a nested base path survives with or without a trailing
slash.

Security is not optional here. Binding to 127.0.0.1 is **not** authorization: any
local process, and any web page able to fetch localhost, could otherwise spend
the user's subscription or read their account status. So every non-health route
requires the local bearer token, browser-ish Origins are rejected, the body is
capped, and concurrency is bounded.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps.helper import paths
from apps.helper.credentials import CredentialError
from apps.helper.inbound import parse_chat_completions
from apps.helper.outbound import render_chat_completion
from apps.helper.registry import CallContext, Registry, default_registry
from apps.helper.types import BadRequest, HelperError, Unauthorized

logger = logging.getLogger("drishti-helper")

DEFAULT_PORT = 8899

#: Chat Completions bodies are prompts, not uploads. 40 MiB matches the upstream
#: proxy ceiling and is far above anything this pipeline sends.
MAX_BODY_BYTES = 40 * 1024 * 1024

#: The pipeline is the only client and it is serial per run; this bound exists to
#: stop a runaway local caller, not to schedule work.
MAX_CONCURRENCY = int(os.environ.get("TA_HELPER_CONCURRENCY", "8"))

_LLM_TIMEOUT_S = float(os.environ.get("TA_HELPER_TIMEOUT_S", "600"))


class RelayManager:
    """Owns the outbound relay connection, driven by the local UI.

    The connection config (portal URL + pairing token) persists in the state
    dir, so a machine reboot reconnects with no user action — the whole point
    of the installed app.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._client: Any = None
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _config_path() -> Path:
        return paths.state_dir() / "relay.json"

    def load_config(self) -> Optional[dict]:
        raw = paths.read_secret(self._config_path())
        if not raw:
            return None
        try:
            cfg = json.loads(raw)
            return cfg if cfg.get("url") and cfg.get("token") else None
        except ValueError:
            return None

    def state(self) -> dict:
        cfg = self.load_config()
        return {
            "configured": cfg is not None,
            "url": (cfg or {}).get("url", ""),
            "connected": bool(self._client and self._client.connected),
            "error": self._client.last_error if self._client else "",
            # Whose runs this connection serves — surfaces a wrong pairing.
            "user": self._client.remote_user if self._client else "",
        }

    async def start(self, url: str, token: str, *, persist: bool = True) -> None:
        from apps.helper.relay_client import RelayClient

        await self.stop()
        if persist:
            paths.write_secret(
                self._config_path(), json.dumps({"url": url, "token": token})
            )
        self._client = RelayClient(url, token, registry=self._registry)
        self._task = asyncio.create_task(self._client.run_forever())

    async def stop(self, *, forget: bool = False) -> None:
        if self._client is not None:
            self._client.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._client = None
        self._task = None
        if forget:
            with contextlib.suppress(FileNotFoundError):
                self._config_path().unlink()

    async def autostart(self) -> None:
        cfg = self.load_config()
        if cfg:
            await self.start(cfg["url"], cfg["token"], persist=False)


#: How often the helper re-asks the portal for the latest version.
_UPDATE_CHECK_INTERVAL_S = 6 * 3600


def _is_newer(latest: str, current: str) -> bool:
    """True only when the portal ships a STRICTLY newer version.

    Plain inequality burned us: a portal running older code told a newer
    helper to "update" to a downgrade. Unparseable versions are treated as
    not-newer — better to miss a banner than to offer a downgrade.
    """
    def parse(v: str) -> Optional[tuple[int, ...]]:
        try:
            return tuple(int(part) for part in v.split("."))
        except ValueError:
            return None

    lp, cp = parse(latest), parse(current)
    if lp is None or cp is None:
        return False
    return lp > cp


def _portal_origin(ws_url: str) -> str:
    """``wss://host/api/relay/ws`` -> ``https://host`` (ws:// -> http://)."""
    scheme = "https" if ws_url.startswith("wss://") else "http"
    host = ws_url.split("://", 1)[-1].split("/", 1)[0]
    return f"{scheme}://{host}"


def _ui_html() -> str:
    """The UI page. PyInstaller unpacks data files under sys._MEIPASS."""
    bundle = getattr(sys, "_MEIPASS", None)
    base = Path(bundle) / "apps" / "helper" if bundle else Path(__file__).parent
    return (base / "ui.html").read_text(encoding="utf-8")


def create_app(
    registry: Optional[Registry] = None,
    *,
    token: Optional[str] = None,
    relay_autostart: bool = False,
) -> FastAPI:
    app = FastAPI(title="drishti-helper", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry or default_registry()
    # Generated on first run and reused; see paths.ensure_local_token.
    app.state.token = token if token is not None else paths.ensure_local_token()
    app.state.sem = asyncio.Semaphore(MAX_CONCURRENCY)
    app.state.relay = RelayManager(app.state.registry)

    from apps.helper.login_flow import LoginFlow

    app.state.login = LoginFlow()
    app.state.update_cache = {"at": 0.0, "info": None}

    if relay_autostart:
        @app.on_event("startup")
        async def _reconnect() -> None:
            await app.state.relay.autostart()

        @app.on_event("shutdown")
        async def _disconnect() -> None:
            await app.state.relay.stop()

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001
        # /ui is the static app page — it carries no secrets (the page reads
        # its API token from the URL fragment the launcher opened it with).
        if request.url.path in ("/healthz", "/ui"):
            return await call_next(request)

        # A page on any site can issue a cross-origin fetch to localhost. Our
        # OWN page's POSTs also carry an Origin header (same-origin ones), so
        # allow exactly that one origin and reject everything else — the
        # legitimate non-browser callers (servers, CLIs) send none.
        origin = request.headers.get("origin")
        own = f"http://{request.headers.get('host', '')}"
        if origin and origin != own:
            return _error(
                Unauthorized(
                    f"cross-origin requests are not accepted (origin {origin!r})",
                    status=403,
                    code="origin_not_allowed",
                )
            )

        auth = request.headers.get("authorization", "")
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        # Compared in constant time so a wrong token cannot be discovered by
        # timing the response.
        import hmac

        if not supplied or not hmac.compare_digest(supplied, request.app.state.token):
            return _error(Unauthorized("missing or invalid helper token"))

        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return _error(
                HelperError("request body too large", status=413, code="payload_too_large")
            )

        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Unauthenticated liveness only — no account data, by design."""
        return {"status": "ok", "providers": request_registry(app).names()}

    async def _provider_state() -> dict[str, Any]:
        out: dict[str, Any] = {
            "permissions_enforced": paths.permissions_enforced(),
            "state_dir": str(paths.state_dir()),
            "providers": {},
        }
        for name in request_registry(app).names():
            provider = request_registry(app).get(name)
            entry: dict[str, Any] = {"adapter": provider.adapter.name}
            try:
                cred = await provider.credential()
                entry.update(
                    ready=True,
                    source=next(
                        (s.name for s in provider.credentials
                         if not callable(getattr(s, "available", None)) or s.available()),
                        "unknown",
                    ),
                    plan=cred.plan,
                    account=cred.account_label,
                    expires_at=cred.expires_at,
                )
            except CredentialError as exc:
                entry.update(ready=False, error=exc.message, remedy=exc.remedy)
            out["providers"][name] = entry
        return out

    @app.get("/v1/status")
    async def status() -> Any:
        """Authenticated. Reports credential health so a user can see, before a
        six-minute run, that their subscription is usable."""
        return await _provider_state()

    # ---- the local UI and its API (all /ui/api routes are bearer-authed) ----

    @app.get("/ui")
    async def ui_page() -> HTMLResponse:
        """The app's control page. Static — the launcher appends the token."""
        return HTMLResponse(_ui_html())

    async def _update_state() -> dict[str, Any]:
        """Compare our version with the portal's, checked at most every 6h.

        Only possible once a relay is configured — that URL is the only portal
        we know about. Failures are silent: an unreachable portal must never
        break the local UI.
        """
        from apps.helper.version import __version__

        cfg = app.state.relay.load_config()
        cache = app.state.update_cache
        if cfg and time.time() - cache["at"] > _UPDATE_CHECK_INTERVAL_S:
            cache["at"] = time.time()
            origin = _portal_origin(cfg["url"])
            try:
                import httpx

                async with httpx.AsyncClient(timeout=4) as http:
                    r = await http.get(f"{origin}/api/helper/version")
                if r.status_code == 200:
                    info = r.json()
                    url = str(info.get("download_url") or "")
                    if url.startswith("/"):
                        url = origin + url
                    cache["info"] = {"version": str(info.get("version") or ""),
                                     "download_url": url}
            except Exception:  # noqa: BLE001
                pass
        info = cache["info"] or {}
        latest = info.get("version", "")
        return {
            "current": __version__,
            "latest": latest,
            "available": _is_newer(latest, __version__),
            "download_url": info.get("download_url", ""),
        }

    @app.get("/ui/api/state")
    async def ui_state() -> Any:
        from apps.helper import autostart

        flow = app.state.login
        return {
            **(await _provider_state()),
            "login": {"status": flow.status, "error": flow.error},
            "relay": app.state.relay.state(),
            "autostart": {"enabled": autostart.enabled()},
            "update": await _update_state(),
        }

    @app.post("/ui/api/autostart")
    async def ui_autostart(request: Request) -> Any:
        from apps.helper import autostart

        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return _error(BadRequest("request body is not valid JSON"))
        if not isinstance(body.get("enabled"), bool):
            return _error(BadRequest("expected {enabled: true|false}"))
        (autostart.enable if body["enabled"] else autostart.disable)()
        return {"enabled": autostart.enabled()}

    @app.post("/ui/api/update/open")
    async def ui_update_open() -> Any:
        """Hand the download to the system browser — the OS webview has no
        download UI, and the browser's is exactly right for a one-off file."""
        import webbrowser

        state = await _update_state()
        if not state["download_url"]:
            return _error(BadRequest("no update download URL known"))
        webbrowser.open(state["download_url"])
        return {"ok": True}

    @app.post("/ui/api/login")
    async def ui_login() -> Any:
        """Kick off the browser sign-in; the UI polls /ui/api/state for the outcome."""
        try:
            url = app.state.login.start()
        except RuntimeError as exc:
            return _error(HelperError(str(exc), status=409, code="login_unavailable"))
        return {"authorize_url": url}

    @app.post("/ui/api/relay")
    async def ui_relay_connect(request: Request) -> Any:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return _error(BadRequest("request body is not valid JSON"))
        url = str(body.get("url") or "").strip()
        tok = str(body.get("token") or "").strip()
        if not url.startswith(("ws://", "wss://")) or not tok:
            return _error(BadRequest("expected {url: ws(s)://..., token: ...}"))
        await app.state.relay.start(url, tok)
        return {"ok": True}

    @app.delete("/ui/api/relay")
    async def ui_relay_disconnect() -> Any:
        await app.state.relay.stop(forget=True)
        return {"ok": True}

    @app.post("/v1/{provider}/chat/completions")
    async def chat_completions(provider: str, request: Request) -> Any:
        reg = request_registry(app)
        prov = reg.get(provider)
        if prov is None:
            return _error(
                BadRequest(
                    f"unknown provider {provider!r}; available: {', '.join(reg.names())}"
                )
            )

        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(
                HelperError("request body too large", status=413, code="payload_too_large")
            )

        try:
            import json

            body = json.loads(raw or b"{}")
        except ValueError:
            return _error(BadRequest("request body is not valid JSON"))

        try:
            req = parse_chat_completions(body)
        except HelperError as exc:
            return _error(exc)

        if req.stream:
            # Returning a non-streaming JSON body for stream:true would silently
            # violate Chat Completions. Refuse loudly instead.
            return _error(
                HelperError(
                    "streaming responses are not implemented; send stream:false",
                    status=501,
                    code="streaming_unsupported",
                )
            )

        try:
            cred = await prov.credential()
        except CredentialError as exc:
            msg = f"{exc.message}. {exc.remedy}".strip()
            return _error(Unauthorized(msg))

        # Cancellation = the caller hung up. The adapter polls is_cancelled
        # between SSE events, so a disconnected client stops the upstream call
        # (and the billing) instead of letting it run to completion. This only
        # sees callers that actually drop the connection — a pipeline that
        # abandons the response without closing it is bounded by timeout_s.
        gone = {"flag": False}

        async def _watch_disconnect() -> None:
            while not gone["flag"]:
                if await request.is_disconnected():
                    gone["flag"] = True
                    return
                await asyncio.sleep(1.0)

        watcher = asyncio.create_task(_watch_disconnect())
        ctx = CallContext(
            timeout_s=_LLM_TIMEOUT_S,
            is_cancelled=lambda: gone["flag"],
        )
        try:
            async with request.app.state.sem:
                resp = await prov.adapter.send(req, cred, prov.quirks, ctx)
        except HelperError as exc:
            return _error(exc)
        except ValueError as exc:  # model resolution — lists valid names
            return _error(BadRequest(str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("adapter failed")
            return _error(HelperError(f"{type(exc).__name__}: {exc}", status=502))
        finally:
            watcher.cancel()

        # Metadata only. Never prompts or attachments.
        logger.info(
            "provider=%s model=%s in=%d out=%d reasoning=%d finish=%s",
            provider, resp.model, resp.usage.prompt_tokens,
            resp.usage.completion_tokens, resp.usage.reasoning_tokens,
            resp.finish_reason,
        )
        return render_chat_completion(
            resp,
            response_id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            created=int(time.time()),
        )

    return app


def request_registry(app: FastAPI) -> Registry:
    return app.state.registry


def _error(exc: HelperError) -> JSONResponse:
    headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
    return JSONResponse(status_code=exc.status, content=exc.envelope(), headers=headers)


app = create_app  # `uvicorn apps.helper.server:app --factory`
