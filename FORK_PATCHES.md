# FORK_PATCHES.md

This repository is a **downstream fork** of the upstream open-source project at
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents). The
upstream is currently configured as the `origin` remote, so see "Git workflow"
below before adding a separate `upstream` remote and pulling.

The fork carries a small number of changes on top of upstream. This document is
the **single source of truth** for every upstream-tracked file the fork modifies,
so a `git merge upstream/main` conflict can be re-applied from one auditable
checklist instead of spelunking through history.

**Default policy:** new features go in **new top-level directories** (e.g.
`webapp/`). Edits to upstream-tracked files are a last resort and must be
small, surgical, and listed in the table below.

---

## Patches to upstream-tracked files

Each entry: file, what changed, why, conflict risk on upstream pulls, whether
to migrate the patch out of the upstream tree.

### 1. `tradingagents/dataflows/utils.py`

**Change:** Added `benchmark_for(ticker)` plus the `_BENCHMARK_BY_SUFFIX` map and
`DEFAULT_BENCHMARK` constant. Maps Yahoo Finance exchange suffixes (`.NS`, `.BO`,
`.TO`, `.T`, `.HK`, `.L`, `.PA`, `.DE`, `.AX`, `.SS`, `.SZ`) to their broad-market
indices. Approximate location: lines 19–46. The pre-existing `safe_ticker_component`
(upstream) follows from line 49 onward, untouched.

**Why:** Indian-market support. Without exchange-aware benchmarking, the
post-trade reflection memory log compared `RELIANCE.NS` against SPY, producing
nonsense alpha figures that future runs would learn from.

**Conflict risk:** Low. Purely additive; upstream is unlikely to add a `benchmark_for`
function or rename `safe_ticker_component`.

**Migration plan:** Keep. The function is called from `trading_graph.py:_fetch_returns`
which is also a fork-modified path (see entry 4); wrapping it in a separate module
would just push the dependency edge into a different file we'd still have to patch.

### 2. `tradingagents/graph/reflection.py`

**Change:** Added a `benchmark: str = "SPY"` parameter to `Reflector.reflect_on_final_decision`
and embedded it in the prompt (`Alpha vs {benchmark}` instead of the previous hardcoded
`Alpha vs SPY`). Approximate location: lines 31–53.

**Why:** The reflection prompt is what the LLM reasons about when writing the
memory-log entry that future runs read. Hardcoding "SPY" labelled non-US trades
incorrectly even after the alpha computation was fixed.

**Conflict risk:** Medium. Upstream may evolve this signature.

**Migration plan:** Keep. Default value preserves prior behavior, so the patch is
non-breaking for upstream callers. If upstream adds new params, merge by hand.

### 3. `tradingagents/graph/trading_graph.py`

**Change:**
- Imported `benchmark_for` (line 21).
- `_fetch_returns` now resolves `benchmark = benchmark_for(ticker)`, fetches the
  index instead of hardcoded `SPY`, and returns a 4-tuple `(raw, alpha, days, benchmark)`
  instead of the prior 3-tuple. Approximate location: lines 191–227.
- `_resolve_pending_entries` unpacks the 4th element and passes `benchmark=` into
  `Reflector.reflect_on_final_decision`. Approximate location: lines 248–256.

**Why:** Wire the benchmark map (entry 1) into the actual return calculation and
into the reflection call (entry 2).

**Conflict risk:** Medium. `_fetch_returns` is a private helper but `trading_graph.py`
is a hot path upstream actively touches.

**Migration plan:** Keep. Wrapping `TradingAgentsGraph` to override one private method
costs more lines than the patch saves.

### 4. `tradingagents/agents/utils/agent_utils.py`

**Change:** One-word edit to `build_instrument_context` — added `.NS` and `.BO` to
the list of exchange suffix examples shown to agents in the prompt. Approximate
location: line 42.

**Why:** Agents receive a prompt fragment listing example exchange suffixes; without
this, the LLM's prior on Indian tickers is weaker.

**Conflict risk:** Low. Single-line, single-string edit.

**Migration plan:** Keep.

### 5. `cli/main.py`

**Change:**
- Added `_RATING_STYLES` color map and `render_rating_panel()` helper. Approximate
  location: lines 640–670.
- `save_report_to_disk` accepts an optional `rating=` kwarg and writes
  `**Final Rating: <rating>**` into the markdown header.
- `display_complete_report` accepts an optional `rating_panel=` and prints it at
  the top of the on-screen report.
- After "Analysis Complete!", the post-analysis flow builds the rating panel from
  the parsed `decision` and prints it. Approximate location: lines 1216–1225.
- Added `RELIANCE.NS` to the inline ticker examples shown at line 506.

**Why:** Surface the 5-tier rating prominently as the one-shot conclusion of an
analysis run. The rating was already being parsed (`graph.process_signal`) but never
displayed.

**Conflict risk:** Medium. `cli/main.py` is large and upstream evolves it.

**Migration plan:** Keep. The webapp has its own banner; this patch is CLI-only and
small enough to re-apply on conflict. If `cli/main.py` becomes unmaintainable across
merges, the rating panel can be deleted (the webapp covers the same need) without
losing functionality.

### 6. `cli/utils.py`

**Change:** Added `RELIANCE.NS` to `TICKER_INPUT_EXAMPLES` at line 11.

**Why:** Tells Indian users at the CLI prompt that the feature is supported.

**Conflict risk:** Low. Single string literal.

**Migration plan:** Keep.

---

## New top-level paths (no upstream collision possible)

Verified upstream does not ship any of these directories or files:

- `webapp/` — FastAPI server, SQLite job store, vanilla-JS frontend, `Dockerfile.webapp`,
  `docker-compose.webapp.yml`, `requirements-webapp.txt`. The entire deployable web
  application lives here.
- `tests/test_benchmark_for.py` — unit tests for `benchmark_for`.
- `FORK_PATCHES.md` — this file.

These never need to be re-applied on an upstream merge.

---

## New tests added

- `tests/test_benchmark_for.py` — 7 unit tests covering the suffix→index map plus
  edge cases (empty/None, unknown suffix, case insensitivity). Pure new file; no
  upstream-merge risk.

---

## Git workflow for upstream pulls

`origin` currently points at the upstream repository. Before merging upstream
changes, set up a separate `upstream` remote so the working `origin` can later
become a true fork remote (e.g. on your own GitHub).

### One-time setup (when forking to your own remote)

```sh
# Rename current origin to upstream
git remote rename origin upstream

# Add your fork as the new origin (replace URL)
git remote add origin git@github.com:<your-org>/TradingAgents.git
git push -u origin main
```

Until that's done, `origin == upstream` and `git pull` already pulls from upstream.

### Pull cadence (recommended monthly or after major upstream releases)

Use a **dedicated tracking branch** so you can inspect what arrived from upstream
before it touches `main`:

```sh
git fetch upstream                               # or `origin` until renamed
git checkout -B upstream-sync upstream/main      # mirror upstream
git checkout main
git merge --no-ff upstream-sync                  # merge, don't rebase
```

We use **merge, not rebase**, so the fork's history shows exactly which upstream
commits arrived and when. Reviewers (and future-you) can `git log --merges` to find
every upstream pull.

### Conflict triage

If `git merge upstream-sync` hits a conflict:

1. Open this file (`FORK_PATCHES.md`).
2. For each conflicted file, check the table above. If the file is listed, the
   patch description tells you what should be there — re-apply by hand.
3. If the conflicted file is **not** listed here, that's a new patch the fork
   accidentally accumulated. Either revert the local edit or add it to the table
   before completing the merge.
4. Run the test suite (`uv run python -m pytest tests/ -q`) before committing
   the merge.

### "Should we just upstream this?"

Some patches are good candidates to send upstream as PRs (e.g., `benchmark_for`,
the rating panel). Doing so eliminates the patch from this file and reduces merge
toil. Track upstream PR numbers in the entry's footer when applicable.

---

## How to add new functionality without growing this file

Default to:

1. **A new file in a new directory.** `webapp/foo.py`, not `tradingagents/foo.py`.
2. **Composition over modification.** Wrap upstream classes; don't subclass them
   inline by editing the upstream module.
3. **Public extension points.** `TradingAgentsGraph(callbacks=[...])` already
   accepts LangChain callbacks — use them instead of patching the class.
4. **Monkey-patching is the last resort**, and only if no extension point exists.
   If you do it, the patch must be applied at webapp startup (not at import time)
   and listed here.

If a new feature genuinely cannot be implemented without editing an upstream file,
add a row to the table above **in the same commit** as the edit.
