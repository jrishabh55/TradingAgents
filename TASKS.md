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
- [x] Move `webapp/` → `apps/api/` (rename and relocate). Update Python module path in:
  - `apps/api/Dockerfile.webapp` (entrypoint `uvicorn webapp.app:app` → `uvicorn apps.api.app:app` or restructure as a package)
  - `apps/api/docker-compose.webapp.yml`
  - All intra-package imports (`from webapp.api.runs import ...` → `from apps.api.api.runs import ...` — note nested `api/` is awkward; consider renaming the inner `api/` subdir to `routes/` while we're moving)
  - `webapp/__init__.py` references
  - `tests/` — search for `from webapp` imports
- [x] Move `my-tanstack-app/` → `apps/web/`. Update:
  - `apps/web/wrangler.jsonc` (Cloudflare paths)
  - `apps/web/Dockerfile` and `docker-compose.fullstack.yml`
  - Any frontend env vars pointing at the API base URL
- [x] Decide on workspace tooling:
  - **Option A:** pnpm workspaces (`pnpm-workspace.yaml` listing `apps/*`) — lightweight, no new tooling
  - **Option B:** Turborepo — better caching, scripts orchestration, but adds a dependency
  - **Recommendation:** start with pnpm workspaces; add Turbo only if build times warrant it
- [x] Update top-level `CLAUDE.md` path lists to match (`webapp/` → `apps/api/`, `my-tanstack-app/` → `apps/web/`)
- [x] Update `FORK_PATCHES.md` if any patches reference `webapp/` paths
- [x] Single squash commit so the rename is reviewable as one diff

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
- [x] Add `request_hash TEXT` column to `runs` schema in `apps/api/jobs/store.py` (or wherever it lives post-restructure). Index on `(request_hash, created_at DESC) WHERE status = 'completed'`.
- [x] Add canonicalize + hash helper in `schemas.py` next to `RunRequest`
- [x] In the POST `/api/runs` handler (`api/runs.py`): compute hash, look up most recent completed run within TTL, return early on hit
- [x] Add `cached: bool = False` to `RunSummary`/`RunDetail`
- [x] Frontend: cache-hit banner — "Cached result, generated N minutes ago. Re-run?" with the force-refresh action
- [x] Tests: cache hit, cache miss, cache hit past TTL, force-refresh bypass, key sensitivity (changing model → miss)

**Bonus:** request coalescing (see task 5) extends the same hash to deduplicate **in-flight** runs.

---

## 3. Auth: replace shared bearer token with real per-user auth ✅ DONE (LIVE)

> Clerk is provisioned and enforcing as of 2026-08-02: `CLERK_JWKS_URL` +
> `CLERK_ISSUER` set on the backend, `VITE_CLERK_PUBLISHABLE_KEY` on the
> frontend, `<ClerkProvider>` + `<UserButton/>` wired in `0ee0b16`. Verified:
> `/health` → 200, `/api/*` → 401 unauthenticated. The legacy shared-bearer and
> open modes still exist as fallbacks — see task 8 for retiring them.



Currently `webapp/app.py:79-90` uses a single shared bearer token via `WEBAPP_AUTH_TOKEN`. Every user is the same identity. No per-user run ownership.

**Decision: Clerk for auth.** Reasons: best DX for B2C, drop-in React components for the frontend, JWT-based so FastAPI verification is ~20 lines, free up to 10k MAU. WorkOS only if enterprise SAML becomes a requirement.

**Work items:**
- [x] Provision Clerk app, get publishable + secret keys, configure JWT template
- [x] Add `user_id TEXT NOT NULL` column to `runs` table (migration; backfill existing rows with a placeholder or drop pre-auth data)
- [x] Replace bearer-token middleware in `apps/api/app.py` with JWT verifier:
  - Fetch Clerk JWKs (cache them)
  - Decode + verify the bearer JWT, extract `user_id` from `sub` claim
  - Attach `user_id` to `request.state` for downstream handlers
- [x] Filter `list_runs()`, `get_run()`, and SSE endpoint by `user_id`
- [x] Frontend: install `@clerk/clerk-react`, wrap app in `<ClerkProvider>`, add `<SignIn/>` + `<SignedIn/>` gates, attach JWT to all API fetches via `useAuth().getToken()`
- [x] Update `WEBAPP_AUTH_TOKEN` to deprecated/removed; document the migration in `apps/api/README.md`
- [x] Tests: unauthenticated requests rejected, cross-user run access denied, JWT signature verification

**Coupled with task 4** — auth without per-user concurrency means User A starting a TSLA run still locks out User B from TSLA. Ship them together or document the limitation.

---

## 4. Per-user concurrency + memory-log isolation ✅ DONE



Today `WEBAPP_CONCURRENCY=1` (default) because parallel runs race the on-disk memory log in the upstream `tradingagents/` core (see comment in `webapp/jobs/runner.py:14-17`). Same-ticker parallelism is also blocked globally via `has_active_run_for_ticker` in `store.py:172-183`.

**Goal:** multiple users analyzing different (or same) tickers in parallel safely.

**Work items:**
- [x] Isolate per-run memory-log directory in `apps/api/integrations/graph_factory.py`. Each `RunRequest` gets a unique scratch dir (e.g. `~/.tradingagents/webapp_logs/<run_id>/`) injected into the graph config. **Do NOT modify the upstream core** — wrap it via the factory's config injection. This is the merge-safe approach.
- [x] Convert global ticker lock to per-user: `has_active_run_for_ticker(ticker, user_id)` instead of `has_active_run_for_ticker(ticker)`. SQL: `WHERE ticker=? AND user_id=? AND status IN ('queued','running')`.
- [x] Bump default `WEBAPP_CONCURRENCY` to something sensible (4? 8?) once isolation is verified
- [x] Stress test: spin up N concurrent runs across different tickers, verify no memory-log corruption (no cross-pollination between agents' reports)
- [x] Update `runner.py:14-17` docstring to remove the "would race the on-disk memory log" warning once fixed

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

## 7. Parity gaps between the API path and upstream's `propagate()`

Surfaced by the 2026-08-02 rebase onto upstream v0.3.1. The runner streams
`graph.graph.stream()` directly instead of calling `TradingAgentsGraph.propagate()`,
so anything upstream wires *inside* `propagate` has to be mirrored in
`apps/api/integrations/graph_factory.py` or we silently don't get it.

**Closed 2026-08-02:**
- [x] Pass `asset_type` — `BTC-USD` was running as `asset_type="stock"`
- [x] Pass `instrument_context` — API runs were missing upstream's
      wrong-company-hallucination fix (`d7b40a2`); agents now get resolved
      company/sector/exchange
- [x] Drop the Fundamentals analyst on crypto tickers, and report the filtered
      list to the frontend so no UI panel waits on an agent that never runs
- [x] `tests/test_graph_factory_state.py` guards all of the above, including a
      signature check that fails when upstream adds a new state field

**Still open:**
- [ ] **`checkpoint_enabled` is a no-op in the API.** Checkpointing is wired
      inside `propagate()` (recompile with a SqliteSaver + inject `thread_id`);
      our path does neither, so `stream_args.config` is just
      `{recursion_limit, callbacks}`. The frontend exposes a toggle that does
      nothing. Either mirror the wiring (note: the checkpointer is a context
      manager whose lifetime must span the whole stream) or hide the toggle.
- [ ] **`past_context` never reaches API runs.** `_run_graph` passes the memory
      log's prior runs for the ticker; we don't. The per-user memory log is
      therefore write-only from the web app's side — CLI runs accumulate
      memory, API runs don't. Product decision: should a user's earlier runs
      colour later ones?
- [ ] **Provider dropdown offers 10 of upstream's 17.** `_PROVIDERS` in
      `apps/api/routes/config.py` is a hand-mirrored copy of
      `cli/utils.py:_llm_provider_table()`. Missing: Bedrock, Groq, Kimi,
      Mistral, NVIDIA NIM, `openai_compatible`, and the `-cn` variants.
      OpenRouter + Azure also render empty model lists (they have no
      `MODEL_OPTIONS` entries; the CLI prompts for a model instead). Low
      urgency — only `OPENAI_API_KEY` is currently set. Consider deriving the
      table from the catalog instead of mirroring it by hand.
- [ ] Stale comment: that file cites `cli/utils.py:233-244`; the table has
      moved to `_llm_provider_table()`.

---

## 8. Retire the legacy `WEBAPP_AUTH_TOKEN` mode

Now that Clerk is live (task 3), the shared-bearer and fully-open modes are
attack surface with no remaining use. Deprecate-then-remove, and rename the
`WEBAPP_*` env vars to `API_*` at the same time (read both, prefer new) — the
rename was deliberately skipped during the restructure to avoid breaking
existing `.env` files.

---

## Done / decided

- **Don't migrate backend to Bun/Elysia.** The `webapp/` job runner is tightly coupled to the in-process LangGraph (`tradingagents/`); a port would split it into two processes (Bun gateway + Python worker) with an IPC boundary, AgentState serialization, and a doubled deploy unit — for no real win since agent runs are LLM-bound, not HTTP-bound. Keep FastAPI; use OpenAPI → TS codegen for typed frontend client if needed.
- **Auth choice:** Clerk (over WorkOS / Better Auth / Supabase / FastAPI-Users). Reconsider if enterprise SAML becomes a requirement.
