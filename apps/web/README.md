# apps/web — TradingAgents frontend

A TanStack Start (React) frontend for the TradingAgents pipeline, deployed to
Cloudflare Workers. Drives the FastAPI backend at `apps/api/` over its REST +
SSE contract, without modifying it.

> **Looking for orientation?** Read [`AGENTS.md`](./AGENTS.md) — it has the
> stack, the design-system primer, the SSE event taxonomy, the env vars, and
> the open work.

## Quick start (local pnpm)

```sh
pnpm install
# In another terminal: start the API (FastAPI) on :8080
#   uvicorn apps.api.app:app --reload --port 8080
pnpm dev
```

Open http://localhost:3000. The dev server proxies `/api/*` to
`WEBAPP1_API_BASE` (default `http://127.0.0.1:8080`).

## Quick start (Docker — full stack)

From the **repo root**:

```sh
docker compose -f apps/web/docker-compose.fullstack.yml up --build
```

- `http://localhost:3000` — TanStack Start frontend (this app)
- `http://localhost:8080` — FastAPI backend (also serves a legacy vanilla-JS UI)

Both UIs drive the same FastAPI backend over its REST + SSE contract. The
two containers share a docker network; this app's `/api/*` proxy targets
`http://backend:8080` via internal DNS. The named volume
`tradingagents_data` is shared with `apps/api/docker-compose.yml` and the
CLI compose, so the SQLite job DB, memory log, and run reports stay
consistent across all three.

The CLI compose at `../../docker-compose.yml` is unchanged.

## Deploy to Cloudflare

```sh
pnpm typecheck
pnpm build
pnpm deploy   # = pnpm build && wrangler deploy
```

Set `WEBAPP1_API_BASE` in `wrangler.jsonc` (or via `wrangler secret put`) to
point at the FastAPI backend you want this worker to proxy to.

## CLAUDE.md / Cursor / agent setup

`AGENTS.md` is the single source of truth — it has the project context plus
the TanStack Intent skill registry baked in. Tools that look for
`CLAUDE.md` will get a thin pointer at `./CLAUDE.md`.
