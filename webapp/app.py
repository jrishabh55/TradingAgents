"""FastAPI application factory for the TradingAgents webapp.

Mount layout:
    /                          → static index.html
    /static/*                  → static assets
    /api/config                → frontend dropdown data
    /api/runs                  → REST CRUD-ish over runs
    /api/runs/{id}/events      → SSE stream

Auth: optional bearer-token gate via ``WEBAPP_AUTH_TOKEN`` env var. If unset, the
app is fully open (intended for personal/internal use behind a VPN). For
public deployments, set the env var and pass ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from webapp.api.config import router as config_router
from webapp.api.runs import router as runs_router
from webapp.api.stream import router as stream_router
from webapp.jobs.bus import get_bus
from webapp.jobs.runner import get_runner, shutdown_runner
from webapp.jobs.store import get_store


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    # Load .env files BEFORE any provider client is instantiated. Mirrors
    # cli/main.py:10-11.
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Initialise singletons. Doing this in the lifespan (not at import time)
        # keeps unit tests in control of WEBAPP_DB_PATH overrides.
        get_store()
        bus = get_bus()
        bus.attach_loop(asyncio.get_running_loop())
        get_runner()
        logger.info("webapp ready")
        try:
            yield
        finally:
            shutdown_runner()

    app = FastAPI(
        title="TradingAgents Webapp",
        version="0.1.0",
        description="Web UI + REST/SSE API on top of the TradingAgents pipeline.",
        lifespan=lifespan,
    )

    # CORS — permissive by default for local-only deploys; restrict via env var.
    cors_origins = _split_csv(os.environ.get("WEBAPP_CORS_ORIGINS", "*"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Bearer-token middleware. Skipped entirely when WEBAPP_AUTH_TOKEN is unset.
    auth_token = os.environ.get("WEBAPP_AUTH_TOKEN", "").strip()
    if auth_token:
        @app.middleware("http")
        async def bearer_token(request: Request, call_next):
            # Don't gate static assets or healthcheck — the SPA needs to load
            # before it can attach the token.
            if request.url.path == "/" or request.url.path.startswith("/static") or request.url.path == "/health":
                return await call_next(request)
            sent = request.headers.get("authorization", "")
            if not sent.startswith("Bearer ") or sent[7:] != auth_token:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
            return await call_next(request)

    # Routes.
    app.include_router(config_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    app.include_router(stream_router, prefix="/api")

    # Static frontend.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        # Container-startup epoch as a cache-bust token. Every container
        # restart yields a new value, which forces browsers to refetch CSS/JS
        # rather than serve a stale, possibly-broken version from cache.
        startup_token = str(int(time.time()))
        index_path = STATIC_DIR / "index.html"

        def _read_index_with_bust() -> str:
            html = index_path.read_text(encoding="utf-8")
            # Append ?v=<token> to references of our own static assets.
            for asset in ("styles.css", "app.js", "marked.min.js", "purify.min.js"):
                src = f"/static/{asset}"
                html = html.replace(src, f"{src}?v={startup_token}")
            return html

        @app.get("/", include_in_schema=False)
        def index() -> Response:
            return HTMLResponse(
                content=_read_index_with_bust(),
                # No-store on the HTML is what makes the cache-bust effective:
                # the browser must fetch a fresh HTML to learn the new ?v=…
                headers={"Cache-Control": "no-store, must-revalidate"},
            )
    else:  # pragma: no cover — only triggers if someone deletes the static dir
        logger.warning("Static directory not found: %s", STATIC_DIR)

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    return app


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


# uvicorn entrypoint (`uvicorn webapp.app:app`).
app = create_app()
