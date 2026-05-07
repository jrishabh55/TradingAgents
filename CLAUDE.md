# TradingAgents — Claude project guide

This repository is a downstream fork of the open-source TradingAgents project. The user is building a productized layer (web UI, API/proxy, deployment) on top of the upstream agent framework, and pulls from upstream regularly.

## Scope of changes

When the user asks for changes, default scope is **the web/product layer only**:

- `apps/api/` — Python FastAPI backend (was `webapp/`)
- `apps/web/` — TanStack frontend (was `my-tanstack-app/`)
- Any new app placed under `apps/` (e.g. a future Bun/Elysia gateway → `apps/gateway/`)
- `deploy/`, `tests/` for product-layer code, and `FORK_PATCHES.md`

The **core CLI and agent framework is off-limits** unless the user explicitly asks for a change to it. Treat these paths as read-only by default:

- `cli/` — interactive CLI entrypoint (`cli/main.py`, `cli/utils.py`)
- `tradingagents/agents/` — agent definitions, memory, tool bindings
- `tradingagents/graph/` — LangGraph orchestration, reflection, signal processing
- `tradingagents/dataflows/` — financial data fetchers (yfinance, finnhub, stockstats, etc.)
- `tradingagents/default_config.py` and other upstream-tracked config

If a web-layer change *seems* to require touching the core, stop and ask first — the user will either authorize the edit explicitly or redirect you to wrap/subclass instead.

## Working with upstream

- Prefer adding new files in new top-level directories over editing upstream files (merge-conflict avoidance).
- Wrap upstream classes via subclassing or composition rather than editing them inline.
- When an upstream-file edit is truly unavoidable, keep it surgical and record it in `FORK_PATCHES.md`.
- Don't refactor upstream code "while you're in there" — the diff cost compounds across every upstream pull.

## Conversation defaults

- "Backend" in this project, unless qualified, means the **web/proxy backend** (`apps/api/`), not the Python agent framework.
- "Frontend" means `apps/web/`.
- "The CLI" or "the agent core" means the `cli/` + `tradingagents/` Python code — only touch it on explicit request.
