# TradingAgents API

A web UI + REST/SSE API on top of the TradingAgents pipeline. Single Docker container, FastAPI backend.

> **Fork-only feature.** This entire `apps/api/` directory is downstream-only. It does not exist upstream and never causes upstream merge conflicts. See `../../FORK_PATCHES.md` for the audit log of upstream-tracked files this fork modifies.

---

## Quick start (Docker)

```sh
# From the repo root
cp .env.example .env          # then edit; add your LLM API keys
docker compose -f apps/api/docker-compose.yml up --build
```

Open <http://localhost:8080>.

## Quick start (local dev)

```sh
# Install API deps (the rest of the project must already be installed)
uv pip install -r apps/api/requirements.txt

# Run with auto-reload
uvicorn apps.api.app:app --reload --port 8080
```

Open <http://localhost:8080>.

---

## What it does

- **Form**: pick ticker, date, analyst team, LLM provider/models, research depth.
- **Live progress**: subscribes to a Server-Sent Events stream and ticks off each agent as it finishes — same shape as the CLI's Rich panel.
- **Result**: rating banner (Strong Buy / Buy / Hold / Reduce / Strong Sell), collapsible per-agent reports, downloadable Markdown.
- **History**: every run is persisted to SQLite and re-viewable.
- **Resume**: closing the browser mid-run is safe; reopening replays the SSE stream from where you left off via `Last-Event-ID`.

---

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `WEBAPP_DB_PATH` | `~/.tradingagents/webapp.sqlite` | SQLite file for runs + events. |
| `WEBAPP_CONCURRENCY` | `10` | Worker pool size. Safe across users (per-user memory logs + per-user run locks). |
| `WEBAPP_KEY_ENCRYPTION_SECRET` | (unset) | Fernet key for encrypting user-pasted BYOC keys (Gemini) at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Unset → the paste-a-key path returns 503. |
| `GOOGLE_CLOUD_PROJECT` | (unset) | GCP project for Gemini-via-Google-account runs (Clerk OAuth token with `cloud-platform` scope → Vertex AI). Only relevant once that path is re-enabled (`OAUTH_ENABLED` in `apps/api/user_keys.py`, currently off — Gemini uses pasted keys only). |
| `WEBAPP_AUTH_TOKEN` | (unset) | Legacy: shared bearer token. Superseded by Clerk JWT when `CLERK_JWKS_URL` is set. |
| `CLERK_JWKS_URL` | (unset) | Clerk JWKS endpoint, e.g. `https://<your-app>.clerk.accounts.dev/.well-known/jwks.json`. When set, every `/api/*` request must carry a valid Clerk JWT. |
| `CLERK_ISSUER` | (unset) | Expected `iss` claim value, e.g. `https://<your-app>.clerk.accounts.dev`. |
| `CLERK_SECRET_KEY` | (unset) | Enables the activation + credits gate: only users with `{"activated": true}` in Clerk privateMetadata may call the API; each run costs 1 credit (seeded to 10). See `CLERK_SETUP.md`. |
| `WEBAPP_CORS_ORIGINS` | `*` | Comma-separated CORS allowlist. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, … | — | LLM provider credentials. Same keys the CLI uses. |
| `TRADINGAGENTS_RESULTS_DIR`, `TRADINGAGENTS_CACHE_DIR`, `TRADINGAGENTS_MEMORY_LOG_PATH` | — | Override upstream paths (memory log, cache). |

Server-side API keys come from environment. The one exception is Gemini, which is BYOC: users paste their own Gemini API key in the UI (stored Fernet-encrypted, see `WEBAPP_KEY_ENCRYPTION_SECRET`). An automatic Google-account path exists but ships disabled (`OAUTH_ENABLED` in `apps/api/user_keys.py`).

---

## REST + SSE API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serves the SPA. |
| `GET` | `/api/config` | Provider/model/analyst options for the UI. |
| `POST` | `/api/runs` | Create + enqueue a run. Body matches `RunRequest`. Returns `RunDetail`. May return an existing run when caching is hit (HTTP 200 + `cached: true`). Pass `?force=true` to bypass the cache. |
| `GET` | `/api/runs` | List runs (most recent first). |
| `GET` | `/api/runs/{id}` | Full run detail incl. final state. |
| `POST` | `/api/runs/{id}/cancel` | Cooperative cancel. Stops after current agent. |
| `GET` | `/api/runs/{id}/report.md` | Download as Markdown. |
| `GET` | `/api/runs/{id}/events` | **SSE** stream. Honours `Last-Event-ID`. |

Schemas live in `apps/api/schemas.py`.

---

## SSE event types

Each event arrives as `event: <type>` plus a JSON `data` payload. Frontend handlers live in `apps/web/src/lib/sse.ts` (and `apps/api/static/app.js` for the legacy vanilla-JS UI).

| Type | When | Key data |
|---|---|---|
| `run.started` | Worker picks up the job. | `ticker`, `analysis_date`, `selected_analysts`, `config` (redacted) |
| `analyst.started` | First chunk where this analyst has no report yet. | `analyst` |
| `analyst.report` | Chunk includes a fresh `*_report` field. | `analyst`, `section`, `content` |
| `analyst.completed` | Analyst's report has arrived. | `analyst` |
| `team.started` | Research / Trading / Risk team begins. | `team` |
| `debate.update` | Bull/Bear/Aggressive/etc. produced new text. | `team`, `role`, `delta`, `full` |
| `team.completed` | Judge produced a verdict. | `team` |
| `report.section` | A non-analyst section finalised. | `section`, `content` |
| `tool.called` | Agent invoked a tool. | `name`, `args` |
| `heartbeat` | 15s keepalive. | — |
| `run.final` | Pipeline finished. Includes parsed rating. | `decision_text`, `rating` |
| `run.failed` | Worker crashed. | `error`, `traceback` |
| `run.cancelled` | Cancel honoured. | — |

Replay: persisted in SQLite. The browser's automatic `Last-Event-ID` reconnect logic gives lossless resumption.

---

## Reverse proxies

If you put nginx / Caddy / Cloudflare in front of this:

- **SSE buffering must be off.** For nginx:
  ```nginx
  location /api/runs {
      proxy_pass http://127.0.0.1:8080;
      proxy_buffering off;
      proxy_cache off;
      proxy_http_version 1.1;
      proxy_set_header Connection "";
  }
  ```
- **Cloudflare** drops idle long-lived connections at 100s. The 15s heartbeat keeps the connection alive; nothing further to configure on your side.
- **Nginx default proxy_read_timeout** is 60s, which kills long SSE streams. Bump to `proxy_read_timeout 86400;` for the runs endpoint.

---

## Operational notes

- **Concurrent by default.** `WEBAPP_CONCURRENCY=10`; per-user memory-log paths plus the runner's per-user lock keep parallel runs isolated. Same-user same-ticker double-submits still get an HTTP 409.
- **API keys are never logged.** The runner redacts any key containing `key`/`token`/`secret` before publishing the `run.started` event. Don't add new ones to the log paths.
- **No mid-run interactivity.** The graph never stops to ask the user a question — every choice is collected upfront in the form and frozen into the run config.
- **Cancel is cooperative**, not preemptive. LangGraph doesn't expose mid-step cancellation; the runner checks the cancel flag between chunks (between agents).

---

## Repo layout

```
apps/api/
├── app.py                          FastAPI factory, lifespan, static mount
├── schemas.py                      Pydantic models
├── routes/
│   ├── config.py                   GET /api/config
│   ├── runs.py                     CRUD over runs + Markdown export
│   └── stream.py                   SSE endpoint with replay + heartbeat
├── jobs/
│   ├── store.py                    SQLite DAO
│   ├── bus.py                      In-process pub/sub
│   ├── translator.py               graph chunk → SSE events
│   └── runner.py                   ThreadPoolExecutor worker
├── integrations/
│   └── graph_factory.py            One-shot upstream wrapper
├── static/
│   ├── index.html                  Legacy SPA shell (apps/web/ is the active UI)
│   ├── app.js                      Vanilla JS, EventSource, single file
│   └── styles.css
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md                       (this file)
```
