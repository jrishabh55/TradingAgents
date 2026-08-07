"""FastAPI application factory for the TradingAgents API.

Mount layout:
    /                          → static index.html
    /static/*                  → static assets
    /api/config                → frontend dropdown data
    /api/runs                  → REST CRUD-ish over runs
    /api/runs/{id}/events      → SSE stream

Auth: Clerk JWT verification when ``CLERK_JWKS_URL`` is set. Falls back to the
legacy shared-bearer token via ``WEBAPP_AUTH_TOKEN`` when only that is set.
With neither set, the app is fully open (intended for personal/internal use
behind a VPN).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from apps.api.auth import auth_middleware, get_verifier
from apps.api.routes.config import router as config_router
from apps.api.routes.runs import router as runs_router
from apps.api.routes.stream import router as stream_router
from apps.api.jobs.bus import get_bus
from apps.api.jobs.runner import get_runner, shutdown_runner
from apps.api.jobs.store import get_store


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
        store = get_store()
        # Any run still queued or running belongs to a process that no longer
        # exists. Sweep BOTH states: a queued row's future died with the previous
        # executor just as surely as a running one, and leaving it queued means it
        # is never picked up and never explained to the user.
        swept = store.sweep_orphaned_runs()
        if swept:
            logger.info("marked %d orphaned run(s) as interrupted", swept)
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

    # Auth: Clerk JWT preferred, legacy shared-bearer fallback, otherwise open.
    # Always installed — populates request.state.user_id for every request, so
    # routes can scope by user without conditional logic. See apps/api/auth.py.
    app.middleware("http")(auth_middleware)
    if get_verifier() is not None:
        logger.info("Clerk auth enabled (CLERK_JWKS_URL configured)")
    elif os.environ.get("WEBAPP_AUTH_TOKEN", "").strip():
        logger.info("Legacy shared-bearer auth enabled (WEBAPP_AUTH_TOKEN set)")
    else:
        logger.info("Auth disabled — every request runs as 'anonymous'")

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


# uvicorn entrypoint (`uvicorn apps.api.app:app`).
app = create_app()
