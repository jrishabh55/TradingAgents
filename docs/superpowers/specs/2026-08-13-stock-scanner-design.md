# Stock Scanner — Design

**Date:** 2026-08-13
**Status:** Approved pending user review
**Scope:** Chartink-style condition scanner for NSE equities, built entirely in the web/product layer (`apps/api/`, `apps/web/`). No changes to `tradingagents/` core or `cli/`.

## Decisions made during brainstorming

- **Universe:** NSE mainboard equities (~2,000 liquid names), single market.
- **Data freshness:** EOD daily bars + 15-minute-delayed intraday bars via yfinance. **No realtime feed in this project.** Realtime (Zerodha Kite or vendor feed) is explicitly deferred; the bar-store boundary is designed so a future feed only replaces ingest.
- **V1 capabilities:** condition engine, ~10 prebuilt scanners, user-defined scanners (CRUD + builder UI), results table, natural-language-to-scanner. **Deferred:** alerts, backtesting, ranking/custom scoring, order-book/microstructure conditions, Ichimoku.
- **Multi-user:** scanners are per-user (Clerk-authed, same ownership model as runs); prebuilt scanners are global (`user_id IS NULL`). Bar data is shared — delayed/EOD public data has no per-user licensing concern.

## Architecture

```
yfinance batch ingest (scheduled)
        ↓
bar store + instruments (SQLite)
        ↓
condition engine (pandas + pandas-ta, vectorized over universe)
        ↓
scanner API (CRUD / run / preview / nl)
        ↓
web UI (gallery, builder, results)
```

New code lives in `apps/api/scanner/` (package) + `apps/api/routes/scanners.py` + `apps/web/src/routes/scanners*`.

## 1. Data layer

### Instruments (universe)

SQLite table `instruments`: `symbol` (NSE code), `yf_symbol` (`.NS`-suffixed), `name`, `sector`, `industry`, `market_cap`, `index_memberships` (JSON array: NIFTY50, NIFTY500, …), `fno` (bool), `updated_at`.

- Seeded from NSE's public equity list (EQ series) filtered to the NIFTY500 + F&O universe by default (configurable); enriched via yfinance `info` (sector/industry/mcap).
- Refreshed weekly by the ingest task. Fundamentals beyond mcap/P/E come from the same yfinance info payload and are stored as a JSON column `fundamentals_json` (pe, pb, roe, dividend_yield, eps, debt_to_equity, revenue_growth — whatever yfinance provides; missing values are NULL and conditions on them simply don't match).

### Bars

SQLite table `bars`: `(symbol, timeframe, ts, open, high, low, close, volume)`, PK `(symbol, timeframe, ts)`. Same SQLite conventions as the runs store (WAL, per-call connections). Separate DB file `scanner.db` in the same mounted volume so scan I/O never contends with run/SSE writes.

- **Timeframes stored:** `1d`, `1h`, `15m`, `5m`. Weekly/monthly are resampled from `1d` at scan time (one pandas resample). No `1m` in v1.
- **Backfill:** first ingest run backfills each timeframe to the retention depth (yfinance serves ~60 days of 15m/5m history, ~2 years of 1h), so intraday historical conditions ("close 5 candles ago", rolling highs, long MAs) work from day one.
- **Retention:** last ~320 bars per (symbol, timeframe) — enough for SMA(200)-class warm-up on every timeframe. Old bars pruned by ingest. Total ≈ 2,000 × 4 × 320 ≈ 2.5M rows ceiling; SQLite is comfortable here. Named upgrade path if scans slow down: DuckDB.

### Ingest

Async background task inside the API process (same pattern as the existing jobs runner — no new container):

- **EOD:** once daily ~18:30 IST, batched `yf.download` (chunks of ~100 symbols) for `1d` bars.
- **Delayed intraday:** every 10 minutes during 09:15–15:30 IST Mon–Fri, fetch `5m`/`15m`/`1h` bars (yfinance interval downloads; data is 15–20 min delayed — surfaced in the UI as "delayed" with a data-as-of timestamp).
- Failures are logged and retried on the next cycle; a partial universe is acceptable (scan runs over whatever bars exist, and the results header shows data-as-of).
- NSE market calendar: a simple weekday + hours check plus a static holiday list for the current year (`scanner/calendar.py`). ponytail: static list, revisit if it ever misfires.

## 2. Condition engine

### Scanner definition = JSON AST

One schema shared by the builder UI, the NL generator, prebuilt seeds, storage, and the API. Formal JSON Schema lives in `apps/api/scanner/schema.py` (Pydantic models, exported as JSON Schema for the NL structured-output call).

```json
{"logic": "AND", "children": [
  {"timeframe": "1d",
   "left":  {"fn": "EMA", "of": "close", "period": 20},
   "op": "crosses_above",
   "right": {"fn": "EMA", "of": "close", "period": 50}},
  {"timeframe": "15m",
   "left":  {"field": "volume"},
   "op": ">",
   "right": {"expr": "*", "args": [{"const": 2}, {"fn": "SMA", "of": "volume", "period": 20}]}},
  {"logic": "OR", "children": ["…nested groups, arbitrary depth…"]}
]}
```

### Operand node types

- `{"const": number}`
- `{"field": name}` — `open, high, low, close, volume, vwap, typical_price, gap_pct, change_pct, body, upper_wick, lower_wick`
- `{"fn": NAME, "of": operand|field, "period": n, …}` — indicator/rolling function
- `{"expr": "+|-|*|/|abs|min|max", "args": […]}` — arithmetic composition
- `{"fundamental": name}` — from `instruments` (market_cap, pe, pb, roe, dividend_yield, eps, debt_to_equity, revenue_growth)
- `{"meta": name}` — classification: sector, industry, index membership, fno (used with `==` / `in`)
- Any operand accepts `"bars_ago": n` (default 0).
- `{"pattern": NAME}` — boolean candlestick pattern operand used with a bare condition (no op/right): doji, hammer, inverted_hammer, shooting_star, hanging_man, bullish_engulfing, bearish_engulfing, morning_star, evening_star, three_white_soldiers, three_black_crows, piercing, dark_cloud_cover.

### Functions (v1 list)

- **Averages:** SMA, EMA, WMA, HMA, VWMA (of price or volume or any operand)
- **Momentum:** RSI, STOCH (%K/%D), STOCHRSI, CCI, WILLR, ROC, MOM
- **Trend:** MACD (line/signal/hist), ADX, SUPERTREND, PSAR
- **Volatility:** ATR, BBANDS (upper/mid/lower), BBWIDTH, STDDEV
- **Flow:** OBV, MFI, CMF
- **Rolling:** HIGHEST, LOWEST, SUM, AVG, COUNT (count of a boolean sub-condition over N bars)

Indicators computed with **`pandas-ta`** (one new Python dependency). Candlestick patterns are hand-rolled vectorized OHLC rules in `scanner/patterns.py` (~3 lines each; no TA-Lib C dependency).

### Condition node

`{"timeframe", "left", "op", "right", "for_n_bars"?}`

- **Operators:** `>`, `<`, `>=`, `<=`, `==`, `!=`, `in` (for meta), `crosses_above`, `crosses_below`.
- **Crosses:** true iff `left > right` at the latest bar AND `left <= right` at the previous bar (vectorized shift comparison).
- **`for_n_bars`: n** — streak wrapper: the comparison must hold on each of the last n bars.
- **Groups:** `{"logic": "AND"|"OR", "children": […]}`, arbitrary nesting.

### Evaluation

`scanner/engine.py`:

1. Walk the AST, collect unique `(timeframe, operand)` pairs (hash-deduped).
2. Load each referenced timeframe's panel from the bar store (long → wide, last ~320 bars × universe symbols).
3. Compute each unique operand series once, per symbol, vectorized (pandas groupby or wide-frame column ops).
4. Evaluate the boolean tree; a symbol matches if the root is true at its latest bar. Symbols missing data for any referenced operand are excluded (never false-positive on missing data).
5. Return matches + the computed operand values per match (feeds the results table's indicator columns).

- **Performance target:** interactive — a typical multi-condition daily scan over 2,000 symbols in ≤ ~3 s. Computed indicator panels cached in-process per `(timeframe, operand-hash)`, invalidated when ingest writes new bars (a bar-store version counter).
- **Limits (trust boundary):** definition JSON ≤ 32 KB, ≤ 50 condition nodes, ≤ 8 nesting depth, periods ≤ 500. Validated by the Pydantic schema before any evaluation.

## 3. API

`apps/api/routes/scanners.py`, Clerk-authed, same ownership conventions as runs (a user sees prebuilt + their own; mutations only on their own).

- `scanners` table: `id`, `user_id` (NULL = prebuilt/global), `name`, `description`, `definition_json`, `created_at`, `updated_at`.
- `GET /scanners` · `POST /scanners` · `PUT /scanners/{id}` · `DELETE /scanners/{id}`
- `POST /scanners/{id}/run` and `POST /scanners/preview` (run an unsaved definition) → `{data_as_of, matches: [{symbol, name, sector, close, change_pct, volume, rvol, values: {…per-operand…}, matched_at}]}` where `rvol` = volume ÷ SMA(volume, 20) on the daily timeframe. Scans run synchronously in the request (they're seconds, not minutes — no job queue).
- `POST /scanners/nl` `{prompt}` → `{definition, explanation}` — LLM structured-output call constrained to the AST JSON Schema, validated server-side, one retry with the validation error appended. Uses the existing per-user LLM key plumbing. **Never auto-runs**; the client loads the result into the builder for review.
- Prebuilt scanners: JSON files in `apps/api/scanner/prebuilt/`, upserted on startup: golden cross, 52-week-high breakout, RSI oversold bounce, volume spike, MACD bullish cross, Bollinger squeeze breakout, supertrend flip, gap-up with volume, above-200-SMA momentum, three white soldiers.

## 4. Frontend

`apps/web/src/routes/`:

- **`/scanners`** — prebuilt gallery + "My scanners" list; run button per scanner; results in a sortable shadcn Table (symbol, price, % change, volume, rvol, plus one column per scan operand). "Data as of HH:MM (delayed)" header. Row click → dialog with the free TradingView symbol widget for **live charts** — the widget streams TradingView's own NSE feed (realtime on TV), independent of our delayed scan bars.
- **`/scanners/new`** (and `/scanners/$id/edit`) — builder page:
  - NL box at top ("Describe your scan…") → calls `/scanners/nl` → fills the form below for review.
  - Manual builder: condition rows (timeframe select · operand picker with params · operator select · right operand/value), AND/OR grouping. AST supports arbitrary nesting; **the UI renders two group levels**, which covers real-world scans.
  - "Preview results" (runs unsaved definition) + Save.
- shadcn/ui components throughout per project convention.

## 5. Testing

- **Engine (the real tests):** synthetic bar fixtures with known answers — cross fires only on the crossing bar; `bars_ago` offsets; `for_n_bars` streaks; multi-timeframe AND; missing-data exclusion; each candlestick pattern's canonical shape; arithmetic expressions; schema limit rejection.
- **NL:** generated-definition schema validation (mock LLM output).
- **API:** ownership/auth tests in the same style as `tests/test_run_cache_ownership.py` (user A cannot read/edit user B's scanner; prebuilts are read-only).

## Deferred (in rough order of expected demand)

1. **Realtime feed** — swaps/augments ingest only; bar store and everything above are unchanged. Feed model (BYO Kite vs vendor) deliberately undecided.
2. **Alerts** — scheduled scan runs + enter/exit diffing per user scanner.
3. **Ranking / custom scoring** — column sorting covers most of it today.
4. **Backtesting** — its own project (entry/exit, P&L stats, historical bar depth ≫ 320).
5. **Order-book / microstructure conditions** — requires a realtime depth feed.
6. **Ichimoku, additional exchanges/universes.**
