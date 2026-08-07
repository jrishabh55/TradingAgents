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
import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.helper import paths
from apps.helper.credentials import CredentialError
from apps.helper.inbound import parse_chat_completions
from apps.helper.outbound import render_chat_completion
from apps.helper.registry import CallContext, Registry, default_registry
from apps.helper.types import BadRequest, HelperError, Unauthorized

logger = logging.getLogger("ta-helper")

DEFAULT_PORT = 8899

#: Chat Completions bodies are prompts, not uploads. 40 MiB matches the upstream
#: proxy ceiling and is far above anything this pipeline sends.
MAX_BODY_BYTES = 40 * 1024 * 1024

#: The pipeline is the only client and it is serial per run; this bound exists to
#: stop a runaway local caller, not to schedule work.
MAX_CONCURRENCY = int(os.environ.get("TA_HELPER_CONCURRENCY", "8"))

_LLM_TIMEOUT_S = float(os.environ.get("TA_HELPER_TIMEOUT_S", "600"))


def create_app(registry: Optional[Registry] = None, *, token: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="ta-helper", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry or default_registry()
    # Generated on first run and reused; see paths.ensure_local_token.
    app.state.token = token if token is not None else paths.ensure_local_token()
    app.state.sem = asyncio.Semaphore(MAX_CONCURRENCY)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001
        if request.url.path == "/healthz":
            return await call_next(request)

        # A page on any site can issue a cross-origin fetch to localhost. Reject
        # anything carrying a browser Origin outright — legitimate callers here
        # are servers and CLIs, which send none.
        origin = request.headers.get("origin")
        if origin:
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

    @app.get("/v1/status")
    async def status() -> Any:
        """Authenticated. Reports credential health so a user can see, before a
        six-minute run, that their subscription is usable."""
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

        ctx = CallContext(
            timeout_s=_LLM_TIMEOUT_S,
            is_cancelled=lambda: False,
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
