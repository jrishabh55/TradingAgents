<!-- intent-skills:start -->
## Skill Loading

Before substantial work:
- Skill check: run `npx @tanstack/intent@latest list`, or use skills already listed in context.
- Skill guidance: if one local skill clearly matches the task, run `npx @tanstack/intent@latest load <package>#<skill>` and follow the returned `SKILL.md`.
- Monorepos: when working across packages, run the skill check from the workspace root and prefer the local skill for the package being changed.
- Multiple matches: prefer the most specific local skill for the package or concern you are changing; load additional skills only when the task spans multiple packages or concerns.
<!-- intent-skills:end -->

# my-tanstack-app — TradingAgents webapp2

## What this is

A TanStack Start (React) frontend that drives the **TradingAgents** pipeline,
deployed to **Cloudflare Workers**. It is the second of two web frontends
sitting in this repo, intentionally left independent of the upstream-fork's
`webapp/` directory:

- `webapp/` (webapp1) — FastAPI + vanilla-JS SPA, single Docker container.
  This is **not modified by webapp2** so the upstream fork stays mergeable.
- `my-tanstack-app/` (webapp2, this directory) — TanStack Start on Cloudflare
  Workers. Talks to the same FastAPI backend (`webapp/app.py`) over its
  REST + SSE contract.

The Python container can be hosted on its own machine or in a separate
Docker container; multiple frontends can share it.

## Scaffold command

```sh
npx @tanstack/cli@latest create my-tanstack-app --agent --tailwind --add-ons cloudflare
# then
cd my-tanstack-app && pnpm install
npx @tanstack/intent@latest install
npx @tanstack/intent@latest list
```

The `--tailwind` flag is deprecated and ignored — Tailwind v4 ships with
TanStack Start scaffolds by default. Lockfile uses pnpm; never reintroduce
`package-lock.json`.

## Stack

- **Framework**: TanStack Start (React 19 + TanStack Router file-based)
- **Bundler**: Vite 8
- **Styling**: Tailwind v4 (CSS-first via `@theme`) + bespoke `.es-*` design tokens
- **UI primitives**: shadcn `radix-nova` preset (`src/components/ui/`)
- **Markdown**: `react-markdown` + `remark-gfm` (renders the agent reports)
- **Deployment target**: Cloudflare Workers (`@cloudflare/vite-plugin`,
  `wrangler.jsonc`)
- **Package manager**: pnpm

## Design system

The visual layer is ported from the **EasyStock** design package (Anthropic
Claude Design handoff bundle). Key files:

- `src/styles.css` — design tokens (`--bg-0..4`, `--text-1..4`, `--accent`,
  `--ok/--run/--err/--info`) and the bespoke classes `.es-card`, `.es-pill`,
  `.es-btn`, `.es-topbar`, `.es-statusbar`, `.es-report`, `.chip`, `.adv-row`,
  `.depth-card`, `.check-card`, `.fld-input`, `.seg`, `.switch`. shadcn
  primitive tokens are aliased onto the same palette.
- The app is **dark-only** — `<html class="dark">` is hardcoded in
  `__root.tsx`. There is no theme toggle.
- Type stack: **Inter** for UI, **JetBrains Mono** for ticker / timestamps /
  terminal-flavoured bits.
- Single accent: **electric blue `#4f7cff`**.

## Flow (the four stages from `flow.jsx`)

1. **Landing** (`/` → `FlowLanding`) — ticker hero, chips for
   Depth/Analysts/Model/Language, recents from `/api/runs`.
2. **Advanced** — same route, expanded inline state. `FlowAdvanced` panels
   for depth (3 cards), analyst checklist, model stack (provider segmented
   control + two model selects + reasoning effort), output language +
   checkpoint resume.
3. **Mid-run** (`/runs/$id` while `status === 'running'`) — `AgentViewLive`
   = V1 Classic Pipeline: 260px tree | report (hero) | 360px activity log.
   Subscribes to `/api/runs/{id}/events` (SSE) via `useRunEvents`.
4. **Done** (`/runs/$id` while `status === 'completed' | 'failed' | 'cancelled'`)
   — `ReadingMode` = V6 Reading Mode: 240px agent rail + centred 760px
   report with Decision / Bull / Bear / Risk / Trade plan / Transcript tabs.

## Backend integration (webapp1 contract)

webapp2 consumes webapp1's REST + SSE API exactly. Source of truth is
`webapp/schemas.py` and `webapp/jobs/translator.py` in the parent repo.

| Endpoint | Used by |
|---|---|
| `GET  /api/config` | `FlowLanding` (provider/model dropdowns) |
| `POST /api/runs` | `FlowLanding.startRun` |
| `GET  /api/runs` | recents list |
| `GET  /api/runs/{id}` | route loader, polling fallback |
| `POST /api/runs/{id}/cancel` | Topbar cancel button |
| `GET  /api/runs/{id}/events` (SSE) | `useRunEvents` hook |
| `GET  /api/runs/{id}/report.md` | Topbar export, ReadingMode export |

SSE event types handled in `src/lib/sse.ts` and reduced into agent state in
`src/components/flow/AgentViewLive.tsx::deriveState`:
`run.started`, `analyst.started`, `analyst.report`, `analyst.completed`,
`team.started`, `team.completed`, `debate.update`, `report.section`,
`tool.called`, `run.final`, `run.failed`, `run.cancelled`. `heartbeat` is
explicitly ignored.

If `webapp/schemas.py` adds or renames a field, mirror the change in
`src/lib/types.ts`. There is no codegen.

## Proxy architecture

The browser only ever sees same-origin `/api/*`. Two proxies do the
forwarding:

- **Dev**: `vite.config.ts`'s `server.proxy['/api']` forwards to
  `WEBAPP1_API_BASE` (default `http://127.0.0.1:8080`).
- **Prod (Cloudflare)**: `src/routes/api.$.tsx` is a server route catch-all
  whose handler streams every request to the same `WEBAPP1_API_BASE`. SSE
  pass-through works because we hand the upstream `Response` straight back
  — Workers stream the body. `duplex: 'half'` is set on bodied requests.

This keeps EventSource working without CORS and avoids a client-side
`Authorization` header on the browser. If you'd rather hit the backend
directly, set `VITE_API_BASE=http://your-backend/api` at build time and the
client uses that origin instead — webapp1 has CORS open by default.

## Environment variables

| Var | Where | Purpose |
|---|---|---|
| `WEBAPP1_API_BASE` | `wrangler.jsonc` (worker) + `vite.config.ts` (dev) | Upstream FastAPI URL the proxy forwards to. |
| `VITE_API_BASE` | build-time | Override frontend's API origin (skips the proxy). Default `/api`. |
| `VITE_API_TOKEN` | build-time | If webapp1 has `WEBAPP_AUTH_TOKEN`, ship the token here. Sent as `Authorization: Bearer …`. |

## Deployment

### Local pnpm

```sh
pnpm install
pnpm dev           # http://localhost:3000, proxies /api to WEBAPP1_API_BASE
pnpm typecheck
pnpm build         # bundles to dist/, including the worker
pnpm deploy        # wrangler deploy (requires `wrangler login` once)
```

### Docker — full stack (webapp1 + webapp2)

```sh
docker compose -f my-tanstack-app/docker-compose.fullstack.yml up --build
```

This brings up two services on a shared `tradingagents_net` network:

- `backend` — FastAPI from `../webapp/Dockerfile.webapp`, port 8080.
- `webapp2` — this app, port 3000.

The webapp2 container's entrypoint (`docker-entrypoint.sh`) rewrites
`wrangler.jsonc`'s `WEBAPP1_API_BASE` with `http://backend:8080` at start,
then runs `vite dev` (which spins up workerd via `@cloudflare/vite-plugin`).
The named volume `tradingagents_data` is shared with the existing
`../webapp/docker-compose.webapp.yml` and the root CLI compose, so all
three (CLI, webapp1, webapp2) read/write the same memory log, SQLite job
DB, and run reports.

`backend` exposes `/health`; webapp2's `depends_on.condition: service_healthy`
ensures Vite only starts after FastAPI is reachable.

### Production (self-hosted Docker — how Dokploy runs it)

`Dockerfile` (no suffix) is the PRODUCTION image: a multi-stage build that
runs `WEB_TARGET=node vite build` (TanStack Start's Node server target —
`WEB_TARGET=node` drops `@cloudflare/vite-plugin`, see `vite.config.ts`) and
serves `dist/` with plain Node via `server-node.mjs` (srvx listener: static
assets from `dist/client`, everything else — SSR + the `/api/$` proxy route —
into the built fetch handler). Two things moved at build time:

- `VITE_CLERK_PUBLISHABLE_KEY` is inlined into the client bundle by
  `vite build`, so it must be a Docker **build arg**, not runtime env.
- `VITE_ALLOWED_HOSTS` is gone in prod — it guarded Vite's dev-server host
  check, which no longer exists.

`WEBAPP1_API_BASE` stays a runtime env var (the proxy route reads it per
request). Dev images (`vite dev` + HMR + workerd) live in `Dockerfile.dev`,
used by `docker-compose.dev.yml` and `docker-compose.fullstack.yml`.

Never run `vite dev` in production: every open tab holds a live HMR
websocket, and a redeploy hot-patches running pages into a corrupted module
graph (this happened; see git history for 2026-08-13).

### Class naming: never anything an adblocker can read as an ad

EasyList ships generic cosmetic filters like `##.adv-label` that hide
matching elements on EVERY site. Our "advanced settings" classes were named
`adv-*`; users with uBlock/AdBlock/AdGuard had the label column
`display:none`d and the whole panel collapsed (localhost is typically
exempt, so dev looked fine). Renamed to `cfg-*` on 2026-08-13. Before
introducing a new class, avoid ad-ish tokens (`ad`, `adv`, `banner`,
`sponsor`, `promo`, `popup`) — or check:
`curl -s https://easylist.to/easylist/easylist.txt | grep '##.your-class'`.

### Production Cloudflare deploy

For prod on Cloudflare Workers, run the FastAPI backend somewhere
reachable, set `WEBAPP1_API_BASE` either in `wrangler.jsonc` `vars`
(non-secret) or via `wrangler secret put WEBAPP1_API_BASE` (secret).
Cloudflare drops idle long-lived connections at 100s; the API's 15s
heartbeat keeps SSE alive.

## Known gotchas

- **Tailwind v4 is CSS-first.** There is no `tailwind.config.ts`.
  Tokens live in `@theme` blocks inside `src/styles.css`. The `--tailwind`
  flag on the scaffolder is a no-op — Tailwind is always on.
- **`@import url(...)` must come before `@import "tailwindcss";`** or
  PostCSS warns. Fonts are imported at the top of `styles.css`.
- **`routeTree.gen.ts` is generated** by the router plugin on first
  `vite dev` / `vite build`. Don't hand-edit it; deleted commits will
  regenerate. It's gitignored by the scaffold.
- **The app is dark-only.** Don't add a theme toggle without first
  confirming with the user — the EasyStock design system has no light
  variant defined.
- **Don't change `webapp/`.** That's webapp1, the fork's existing
  upstream-mergeable surface. Cross-cutting backend work belongs in
  `webapp/` (Python); webapp2 stays read-only on its API contract.
- **SSE replay** uses the browser's automatic `Last-Event-ID` reconnect.
  webapp1 persists events in SQLite for replay. If you swap the EventSource
  for a custom fetch-based reader, preserve the resume contract.

## Next steps (open work)

- Re-run / Compare buttons on `ReadingMode` are present but not wired.
- The agent rail in `ReadingMode` doesn't yet expose per-agent transcripts.
- No tests yet beyond what `vitest` + `@testing-library/react` ship with.
- Auth is bearer-only and read from a build-time env var; for multi-user
  deploys add a real auth provider (Cloudflare Access, Clerk, etc.) via the
  `router-core/auth-and-guards` skill.
