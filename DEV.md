# Development workflow

Two ways to run the web layer locally:
- **Docker (recommended for full stack)** — spins up backend + frontend
  with hot reload, no host-side Python or Node setup needed.
- **Native (faster iteration on a single side)** — run uvicorn or pnpm
  dev directly on the host.

The CLI and the agent core (`cli/`, `tradingagents/`) are unaffected by
both — they run via the upstream Python entrypoint as before.

---

## Docker dev — single command

```sh
docker compose -f docker-compose.dev.yml up --build
```

What that gives you:
| Service | URL | Hot reload |
|---|---|---|
| Frontend (TanStack + Vite) | http://localhost:3000 | yes — Vite HMR on `apps/web/src/**` |
| API (FastAPI + uvicorn) | http://localhost:8080 | yes — uvicorn `--reload` on `apps/api/`, `tradingagents/`, `cli/` |

First boot takes ~3 minutes (pip install + pnpm install). Subsequent boots
reuse the layer cache and start in ~5 seconds.

### Source edits

Edit any `.py` under `apps/api/`, `tradingagents/`, or `cli/` — uvicorn
detects the change and reloads in <1 second. Edit any `.ts`/`.tsx` under
`apps/web/src/` — Vite HMR pushes the new module to the browser.

You **don't need to rebuild the image** for source changes. Source is
bind-mounted from the host repo into the container; the editable Python
install (`pip install -e .` in `Dockerfile.dev`) makes the bind-mount the
authoritative version of the package.

### When to rebuild

Rebuild only when dependencies change:
```sh
# Python deps changed (apps/api/requirements.txt or pyproject.toml)
docker compose -f docker-compose.dev.yml up --build api

# JS deps changed (apps/web/package.json)
docker compose -f docker-compose.dev.yml up --build web
```

### Running tests inside the container

```sh
docker compose -f docker-compose.dev.yml exec api pytest tests/ -q
```

The repo is bind-mounted, so changes to test files take effect
immediately — no rebuild between iterations.

### Reset state

The SQLite job DB, per-run report archive, and per-user memory logs live
in a named volume `tradingagents_data_dev`. To wipe:

```sh
docker compose -f docker-compose.dev.yml down -v
```

`-v` removes the volume. Without `-v`, `down` keeps the data so
`up` again returns you to your previous state.

### Logs

```sh
docker compose -f docker-compose.dev.yml logs -f api    # tail API logs
docker compose -f docker-compose.dev.yml logs -f web    # tail Vite output
docker compose -f docker-compose.dev.yml logs -f        # both
```

---

## Native dev — when you only need one side

### API only

```sh
# Project root
uv run uvicorn apps.api.app:app --reload --port 8080
```

Reads env from `.env` at the project root (CLERK_*, LLM keys, etc.).

### Frontend only

```sh
cd apps/web
pnpm install     # first time
pnpm dev         # runs Vite at http://localhost:3000
```

Reads env from `apps/web/.env` (`VITE_CLERK_PUBLISHABLE_KEY`, etc.). The
Vite proxy forwards `/api/*` to `WEBAPP1_API_BASE` (default
`http://127.0.0.1:8080`), so you typically run the API natively in another
terminal at the same time.

---

## Env vars

Two env files matter for dev:

### `.env` at the repo root

Used by:
- The native `uv run uvicorn ...` flow (loaded by `python-dotenv` in `apps/api/app.py`)
- The Docker dev compose (passed via `env_file: - .env` in
  `docker-compose.dev.yml`)

What goes here:
- LLM provider API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.)
- Clerk auth: `CLERK_JWKS_URL`, `CLERK_ISSUER`
- Optional overrides: `WEBAPP_DB_PATH`, `WEBAPP_CONCURRENCY`,
  `WEBAPP_CACHE_TTL_SECONDS`, etc.

See `.env.example` for the template.

### `apps/web/.env`

Used by:
- Vite dev (native and inside the container)

What goes here:
- `VITE_CLERK_PUBLISHABLE_KEY` (the `VITE_` prefix is required for Vite to
  expose the value to the browser bundle)
- `WEBAPP1_API_BASE` (Cloudflare Worker dev proxy target)

`apps/web/.env` is **never read by the API**. Don't put secret keys here
that the API needs — they'd be invisible.

---

## Common gotchas

### "ModuleNotFoundError: No module named 'apps'" on the host

The pytest config at `pyproject.toml` adds the project root to
`pythonpath`, so `pytest` works from the project root. If you're running
a script directly:

```sh
PYTHONPATH=. uv run python script.py
```

Inside the Docker container, `apps.api.X` is importable via the editable
install — no PYTHONPATH dance needed.

### "Permission denied" on the SQLite DB

If you previously ran `apps/api/docker-compose.yml` (the prod compose)
and now switch to the dev compose, they use **different named volumes**
on purpose:
- prod: `tradingagents_data`
- dev: `tradingagents_data_dev`

If you want to share data between the two (e.g. seed dev with prod
runs), explicitly mount the same volume name in both compose files —
but be aware they were not designed to be cross-mounted.

### Vite says it can't find a module right after a fresh clone

The frontend `apps/web/src/lib/` directory was historically silently
gitignored by an unanchored `lib/` rule in the project root `.gitignore`
(fixed in commit `dfacde9`). If you have an old clone, pull and verify:

```sh
ls apps/web/src/lib/
# Should show: api.ts sse.ts teams.ts types.ts utils.ts
```

If those files are missing, `git pull` to get the .gitignore fix, then
restore from a coworker's working copy or re-checkout the files from
`HEAD`.

### Hot reload doesn't seem to fire on macOS

Docker Desktop on macOS uses gRPC-FUSE (or VirtioFS) for bind mounts.
File events propagate but with a slight delay (~1s). If reload feels
totally broken, check that:
1. Your edit actually saved (the IDE reports back).
2. The reload-dir in `apps/api/Dockerfile.dev` includes the file's
   parent (currently `apps/api`, `tradingagents`, `cli` only).
3. `WATCHFILES_FORCE_POLLING=1` env var, if set, will work but is
   slower.

If you keep running into this, switch to native dev for the Python side
(faster iteration) and use Docker only for the frontend.
