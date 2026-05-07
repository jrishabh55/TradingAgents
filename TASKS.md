# Web layer tasks

Backlog for the web/product layer (`webapp/` + `my-tanstack-app/`). The CLI and `tradingagents/` core are out of scope unless explicitly noted — see CLAUDE.md.

Order is rough priority; each item is independently shippable.

---

## 1. Restructure: `apps/` monorepo layout ✅ DONE



Move the web layer into a clearer monorepo shape so frontend and backend live side by side.

**Target layout:**
```
apps/
  api/    ← was webapp/  (FastAPI backend, renamed from "webapp")
  web/    ← was my-tanstack-app/  (TanStack frontend)
```

**Why:** the current `webapp/` name conflates "web app" with "backend API" and lives at a different depth than `my-tanstack-app/`. An `apps/` umbrella matches the standard monorepo convention (Turbo/Nx/pnpm workspaces) and makes the frontend↔backend pairing obvious.

**Work items:**
- [ ] Move `webapp/` → `apps/api/` (rename and relocate). Update Python module path in:
  - `apps/api/Dockerfile.webapp` (entrypoint `uvicorn webapp.app:app` → `uvicorn apps.api.app:app` or restructure as a package)
  - `apps/api/docker-compose.webapp.yml`
  - All intra-package imports (`from webapp.api.runs import ...` → `from apps.api.api.runs import ...` — note nested `api/` is awkward; consider renaming the inner `api/` subdir to `routes/` while we're moving)
  - `webapp/__init__.py` references
  - `tests/` — search for `from webapp` imports
- [ ] Move `my-tanstack-app/` → `apps/web/`. Update:
  - `apps/web/wrangler.jsonc` (Cloudflare paths)
  - `apps/web/Dockerfile` and `docker-compose.fullstack.yml`
  - Any frontend env vars pointing at the API base URL
- [ ] Decide on workspace tooling:
  - **Option A:** pnpm workspaces (`pnpm-workspace.yaml` listing `apps/*`) — lightweight, no new tooling
  - **Option B:** Turborepo — better caching, scripts orchestration, but adds a dependency
  - **Recommendation:** start with pnpm workspaces; add Turbo only if build times warrant it
- [ ] Update top-level `CLAUDE.md` path lists to match (`webapp/` → `apps/api/`, `my-tanstack-app/` → `apps/web/`)
- [ ] Update `FORK_PATCHES.md` if any patches reference `webapp/` paths
- [ ] Single squash commit so the rename is reviewable as one diff

**Risks:** breaks every `webapp.*` import in one go. Run the full test suite + boot the dev server before merging.

---

## 2. Run caching by request hash ✅ DONE



Cache completed runs by a hash of the canonicalized `RunRequest`, so identical requests skip the pipeline and return the existing report.

**Why:** each run costs $0.50–$5+ in LLM tokens; identical re-runs are pure waste. The reports are already persisted in `final_state_json` — the cache is the runs table, we just need a lookup.

**Design:**
- **Cache key:** SHA-256 over canonicalized request — sort `analysts`, lowercase `llm_provider`, drop `checkpoint_enabled` (internal mechanism, doesn't affect output), JSON-encode with `sort_keys=True`
- **TTL:** 24h default (configurable via env). Trading data shifts daily — news/sentiment tools fetch up to "now," not up to `analysis_date`, so a stale cache delivers misleading analysis
- **Hit semantics:** POST `/api/runs` with cache hit returns `{id: existing_id, cached: true}` HTTP 200 instead of 201. Frontend navigates to the existing detail page.
- **Scope:** shared cache (no `user_id` filter). Reports are public-data analyses; no privacy leak. Maximizes savings.
- **Force refresh:** `POST /api/runs?force=true` bypasses cache.

**Work items:**
- [ ] Add `request_hash TEXT` column to `runs` schema in `apps/api/jobs/store.py` (or wherever it lives post-restructure). Index on `(request_hash, created_at DESC) WHERE status = 'completed'`.
- [ ] Add canonicalize + hash helper in `schemas.py` next to `RunRequest`
- [ ] In the POST `/api/runs` handler (`api/runs.py`): compute hash, look up most recent completed run within TTL, return early on hit
- [ ] Add `cached: bool = False` to `RunSummary`/`RunDetail`
- [ ] Frontend: cache-hit banner — "Cached result, generated N minutes ago. Re-run?" with the force-refresh action
- [ ] Tests: cache hit, cache miss, cache hit past TTL, force-refresh bypass, key sensitivity (changing model → miss)

**Bonus:** request coalescing (see task 5) extends the same hash to deduplicate **in-flight** runs.

---

## 3. Auth: replace shared bearer token with real per-user auth

Currently `webapp/app.py:79-90` uses a single shared bearer token via `WEBAPP_AUTH_TOKEN`. Every user is the same identity. No per-user run ownership.

**Decision: Clerk for auth.** Reasons: best DX for B2C, drop-in React components for the frontend, JWT-based so FastAPI verification is ~20 lines, free up to 10k MAU. WorkOS only if enterprise SAML becomes a requirement.

**Work items:**
- [ ] Provision Clerk app, get publishable + secret keys, configure JWT template
- [ ] Add `user_id TEXT NOT NULL` column to `runs` table (migration; backfill existing rows with a placeholder or drop pre-auth data)
- [ ] Replace bearer-token middleware in `apps/api/app.py` with JWT verifier:
  - Fetch Clerk JWKs (cache them)
  - Decode + verify the bearer JWT, extract `user_id` from `sub` claim
  - Attach `user_id` to `request.state` for downstream handlers
- [ ] Filter `list_runs()`, `get_run()`, and SSE endpoint by `user_id`
- [ ] Frontend: install `@clerk/clerk-react`, wrap app in `<ClerkProvider>`, add `<SignIn/>` + `<SignedIn/>` gates, attach JWT to all API fetches via `useAuth().getToken()`
- [ ] Update `WEBAPP_AUTH_TOKEN` to deprecated/removed; document the migration in `apps/api/README.md`
- [ ] Tests: unauthenticated requests rejected, cross-user run access denied, JWT signature verification

**Coupled with task 4** — auth without per-user concurrency means User A starting a TSLA run still locks out User B from TSLA. Ship them together or document the limitation.

---

## 4. Per-user concurrency + memory-log isolation

Today `WEBAPP_CONCURRENCY=1` (default) because parallel runs race the on-disk memory log in the upstream `tradingagents/` core (see comment in `webapp/jobs/runner.py:14-17`). Same-ticker parallelism is also blocked globally via `has_active_run_for_ticker` in `store.py:172-183`.

**Goal:** multiple users analyzing different (or same) tickers in parallel safely.

**Work items:**
- [ ] Isolate per-run memory-log directory in `apps/api/integrations/graph_factory.py`. Each `RunRequest` gets a unique scratch dir (e.g. `~/.tradingagents/webapp_logs/<run_id>/`) injected into the graph config. **Do NOT modify the upstream core** — wrap it via the factory's config injection. This is the merge-safe approach.
- [ ] Convert global ticker lock to per-user: `has_active_run_for_ticker(ticker, user_id)` instead of `has_active_run_for_ticker(ticker)`. SQL: `WHERE ticker=? AND user_id=? AND status IN ('queued','running')`.
- [ ] Bump default `WEBAPP_CONCURRENCY` to something sensible (4? 8?) once isolation is verified
- [ ] Stress test: spin up N concurrent runs across different tickers, verify no memory-log corruption (no cross-pollination between agents' reports)
- [ ] Update `runner.py:14-17` docstring to remove the "would race the on-disk memory log" warning once fixed

**Why this isn't trivial:** the upstream memory log is FAISS+SQLite+JSON files at module-scope paths. Need to verify all three honor the per-run directory injection. Worst case: one of them is a hardcoded path and we need a `FORK_PATCHES.md` entry to parameterize it — keep that diff minimal.

---

## 5. Request coalescing for concurrent identical runs

Extends task 2: if two users submit the same `request_hash` while one is still **running**, attach the second user's SSE stream to the in-flight run instead of starting a new pipeline.

**Why:** caching solves "User A finished, User B starts the same query." Coalescing solves "User A and User B start the same query within seconds." Without it, you run the expensive pipeline twice for the same answer.

**Work items:**
- [ ] On POST `/api/runs`, after the cache lookup, also check for `status IN ('queued', 'running')` runs with matching `request_hash`
- [ ] If found: return that run id, frontend connects to its SSE stream as if it had submitted it
- [ ] Decide on ownership: does the coalesced run get a duplicate row (per-user view) or do both users share the original? Probably duplicate row pointing at original via `coalesced_from`, similar to the cache `cached_from` link
- [ ] Tests: concurrent-submit dedup, error propagation (if upstream run fails, do followers see the failure?), cancel semantics (does User A canceling kill the run if User B is also watching? — probably no, only the last viewer cancel triggers actual cancel)

---

## 6. (Defer) Subscription / billing layer

If this product goes paid (Clerk + RevenueCat? Stripe direct?), think about quota:
- Free tier: N runs/day, only Haiku models
- Paid tier: unlimited runs, all models
- Per-user run counter on the runs table (already implicit once `user_id` exists)

Not urgent. Note here so we don't forget the schema needs a `subscription_tier` or similar when the time comes.

---

## Done / decided

- **Don't migrate backend to Bun/Elysia.** The `webapp/` job runner is tightly coupled to the in-process LangGraph (`tradingagents/`); a port would split it into two processes (Bun gateway + Python worker) with an IPC boundary, AgentState serialization, and a doubled deploy unit — for no real win since agent runs are LLM-bound, not HTTP-bound. Keep FastAPI; use OpenAPI → TS codegen for typed frontend client if needed.
- **Auth choice:** Clerk (over WorkOS / Better Auth / Supabase / FastAPI-Users). Reconsider if enterprise SAML becomes a requirement.
