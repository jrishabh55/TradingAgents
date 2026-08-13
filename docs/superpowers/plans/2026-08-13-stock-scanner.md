# Stock Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chartink-style condition scanner for NSE equities — bar store + yfinance ingest, JSON-AST condition engine, scanner CRUD API, builder UI, NL-to-scanner.

**Architecture:** New `apps/api/scanner/` package (SQLite bar/instrument/scanner store, pandas wide-frame engine) + `apps/api/routes/scanners.py` + TanStack routes under `apps/web/src/routes/scanners*`. Ingest is an async task in the API process. Spec: `docs/superpowers/specs/2026-08-13-stock-scanner-design.md`.

**Tech Stack:** FastAPI, Pydantic v2, SQLite (WAL), pandas + numpy (already deps — **no new Python dependencies**), yfinance, langchain-openai (NL), React 19 + TanStack Router + shadcn/ui.

## Global Constraints

- **Never touch** `cli/`, `tradingagents/` — all code lives in `apps/api/`, `apps/web/`, `tests/`.
- Separate DB file: env `SCANNER_DB_PATH`, default `data/scanner.sqlite`. Never share the runs DB.
- Timeframes stored: `5m`, `15m`, `1h`, `1d`. `1w`/`1mo` resampled from `1d` at scan time.
- Retention: **320 bars** per (symbol, timeframe).
- Definition limits (validate before evaluating): JSON ≤ **32768 bytes**, ≤ **50** condition nodes, nesting depth ≤ **8**, any period ≤ **500**.
- Missing data never matches: a symbol lacking data for any referenced operand is excluded.
- Auth: every route uses `Depends(current_user_id)` from `apps.api.auth`; prebuilt scanners have `user_id IS NULL` and are read-only via the API.
- Python tests: `pytest tests/test_scanner_*.py -v` (repo root). Frontend: run inside `apps/web/`.
- Commit after every task (steps say when). Use `--no-verify` only if hooks block on unrelated files.

## File Map

| File | Responsibility |
|---|---|
| `apps/api/scanner/__init__.py` | empty package marker |
| `apps/api/scanner/store.py` | SQLite: instruments, bars, scanners; singleton |
| `apps/api/scanner/schema.py` | Pydantic AST models, limits, JSON Schema export |
| `apps/api/scanner/calendar.py` | NSE market hours / holidays |
| `apps/api/scanner/indicators.py` | Panel + operand→frame evaluation |
| `apps/api/scanner/patterns.py` | candlestick pattern bool frames |
| `apps/api/scanner/engine.py` | AST evaluation, caching, result building |
| `apps/api/scanner/universe.py` | universe CSV seed + yfinance enrichment |
| `apps/api/scanner/ingest.py` | yfinance batch ingest + async loop |
| `apps/api/scanner/prebuilt/*.json` | 10 seed scanners |
| `apps/api/scanner/nl.py` | NL → definition via LLM |
| `apps/api/routes/scanners.py` | REST API |
| `apps/api/app.py` | wire router + ingest task (modify) |
| `apps/web/src/lib/scanner-types.ts` | TS types for AST/results |
| `apps/web/src/lib/scanner-rows.ts` | builder rows ⇄ AST converters |
| `apps/web/src/lib/api.ts` | add scanner methods (modify) |
| `apps/web/src/components/scanner/ResultsTable.tsx` | sortable results + TradingView dialog |
| `apps/web/src/components/scanner/ScannerBuilder.tsx` | condition builder form |
| `apps/web/src/routes/scanners.index.tsx` | gallery + run + results |
| `apps/web/src/routes/scanners.new.tsx`, `scanners.$id.edit.tsx` | create/edit pages |
| `tests/scanner_utils.py` | shared synthetic-bar helpers |
| `tests/test_scanner_{store,schema,calendar,indicators,patterns,engine,universe,ingest,api,nl}.py` | tests |

---

### Task 1: Scanner store

**Files:**
- Create: `apps/api/scanner/__init__.py` (empty), `apps/api/scanner/store.py`
- Test: `tests/test_scanner_store.py`, `tests/scanner_utils.py`

**Interfaces:**
- Produces: `ScannerStore` with methods used by every later task:
  - `upsert_instruments(rows: list[dict]) -> None`, `instruments_df() -> pd.DataFrame` (index `symbol`; columns `yf_symbol,name,sector,industry,market_cap,index_memberships(list),fno(bool),fundamentals(dict)`)
  - `upsert_bars(timeframe: str, df: pd.DataFrame) -> None` (long df: `symbol,ts,open,high,low,close,volume`; ts ISO str), `load_bars(timeframe, limit=320) -> pd.DataFrame` (same long shape, ts ascending), `prune_bars(timeframe, keep=320)`, `latest_ts(timeframe) -> str | None`, `version() -> int` (bumps on every `upsert_bars`)
  - scanners: `create_scanner(user_id, name, description, definition: dict) -> str`, `get_scanner(sid) -> dict | None`, `list_scanners(user_id) -> list[dict]` (prebuilt + own), `update_scanner(sid, name, description, definition) -> None`, `delete_scanner(sid) -> None`, `upsert_prebuilt(name, description, definition) -> None` (keyed by name)
  - module fns: `get_scanner_store() -> ScannerStore`, `reset_scanner_store_for_tests(path) -> ScannerStore`

- [ ] **Step 1: Write shared test helpers**

`tests/scanner_utils.py`:

```python
"""Synthetic-bar helpers shared by scanner tests."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from apps.api.scanner.store import ScannerStore, reset_scanner_store_for_tests


def bars_long(symbol: str, closes: list[float], *, start: str = "2026-01-05",
              volumes: list[float] | None = None, freq_minutes: int | None = None,
              opens: list[float] | None = None, highs: list[float] | None = None,
              lows: list[float] | None = None) -> pd.DataFrame:
    """Long-format bars from a close series. Daily by default; freq_minutes for intraday."""
    n = len(closes)
    t0 = datetime.fromisoformat(start)
    if freq_minutes:
        ts = [(t0 + timedelta(minutes=freq_minutes * i)).isoformat() for i in range(n)]
    else:
        ts = [(t0 + timedelta(days=i)).isoformat() for i in range(n)]
    o = opens if opens is not None else [closes[max(i - 1, 0)] for i in range(n)]
    h = highs if highs is not None else [max(a, b) * 1.001 for a, b in zip(o, closes)]
    l = lows if lows is not None else [min(a, b) * 0.999 for a, b in zip(o, closes)]
    v = volumes if volumes is not None else [1000.0] * n
    return pd.DataFrame({"symbol": symbol, "ts": ts, "open": o, "high": h,
                         "low": l, "close": closes, "volume": v})


def make_store(tmp_path, data: dict[str, list[float]] | None = None,
               timeframe: str = "1d") -> ScannerStore:
    """Fresh store; optionally seed instruments + bars from {symbol: closes}."""
    store = reset_scanner_store_for_tests(tmp_path / "scanner.sqlite")
    if data:
        store.upsert_instruments([
            {"symbol": s, "yf_symbol": f"{s}.NS", "name": s, "sector": "Test",
             "industry": "Test", "market_cap": 5000.0, "index_memberships": ["NIFTY500"],
             "fno": False, "fundamentals": {"pe": 20.0}}
            for s in data
        ])
        for s, closes in data.items():
            store.upsert_bars(timeframe, bars_long(s, closes))
    return store
```

`tests/test_scanner_store.py`:

```python
from __future__ import annotations

from tests.scanner_utils import bars_long, make_store


def test_bars_roundtrip_and_version(tmp_path):
    store = make_store(tmp_path)
    assert store.version() == 0
    assert store.latest_ts("1d") is None
    store.upsert_bars("1d", bars_long("TCS", [100, 101, 102]))
    assert store.version() == 1
    df = store.load_bars("1d")
    assert list(df["close"]) == [100, 101, 102]
    assert df["ts"].is_monotonic_increasing
    assert store.latest_ts("1d") == df["ts"].iloc[-1]
    # Idempotent upsert: same PK rows replace, not duplicate.
    store.upsert_bars("1d", bars_long("TCS", [100, 101, 102]))
    assert len(store.load_bars("1d")) == 3


def test_prune_keeps_newest(tmp_path):
    store = make_store(tmp_path)
    store.upsert_bars("1d", bars_long("TCS", list(range(400))))
    store.prune_bars("1d", keep=320)
    df = store.load_bars("1d", limit=500)
    assert len(df) == 320
    assert df["close"].iloc[-1] == 399


def test_instruments_roundtrip(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0]})
    inst = store.instruments_df()
    assert inst.loc["TCS", "yf_symbol"] == "TCS.NS"
    assert inst.loc["TCS", "index_memberships"] == ["NIFTY500"]
    assert inst.loc["TCS", "fundamentals"]["pe"] == 20.0


def test_scanner_crud_and_scoping(tmp_path):
    store = make_store(tmp_path)
    d = {"logic": "AND", "children": []}
    sid = store.create_scanner("user_a", "My scan", "desc", d)
    store.upsert_prebuilt("Golden cross", "pb", d)
    store.upsert_prebuilt("Golden cross", "pb updated", d)  # keyed by name

    a = store.list_scanners("user_a")
    b = store.list_scanners("user_b")
    assert {s["name"] for s in a} == {"My scan", "Golden cross"}
    assert {s["name"] for s in b} == {"Golden cross"}
    assert store.get_scanner(sid)["user_id"] == "user_a"

    store.update_scanner(sid, "Renamed", "d2", d)
    assert store.get_scanner(sid)["name"] == "Renamed"
    store.delete_scanner(sid)
    assert store.get_scanner(sid) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scanner_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.api.scanner'`

- [ ] **Step 3: Implement the store**

`apps/api/scanner/__init__.py`: empty file.

`apps/api/scanner/store.py`:

```python
"""SQLite persistence for the scanner: instruments, bars, scanner definitions.

Separate DB file from the runs store so bulk bar writes never contend with
run/SSE traffic. Same conventions as apps/api/jobs/store.py: WAL mode,
per-call connections.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    yf_symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    sector TEXT,
    industry TEXT,
    market_cap REAL,
    index_memberships TEXT NOT NULL DEFAULT '[]',
    fno INTEGER NOT NULL DEFAULT 0,
    fundamentals_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_tf_ts ON bars (timeframe, ts);
CREATE TABLE IF NOT EXISTS scanners (
    id TEXT PRIMARY KEY,
    user_id TEXT,                 -- NULL = prebuilt/global
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_BAR_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScannerStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    # -- bars ------------------------------------------------------------
    def upsert_bars(self, timeframe: str, df: pd.DataFrame) -> None:
        rows = [(r.symbol, timeframe, r.ts, r.open, r.high, r.low, r.close, r.volume)
                for r in df.itertuples(index=False)]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO bars (symbol,timeframe,ts,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            c.execute(
                "INSERT INTO meta (k, v) VALUES ('bar_version', '1') "
                "ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)")

    def load_bars(self, timeframe: str, limit: int = 320) -> pd.DataFrame:
        # Newest `limit` bars per symbol, returned ascending.
        q = ("SELECT symbol, ts, open, high, low, close, volume FROM ("
             "  SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) rn"
             "  FROM bars WHERE timeframe = ?"
             ") WHERE rn <= ? ORDER BY symbol, ts")
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=(timeframe, limit))

    def prune_bars(self, timeframe: str, keep: int = 320) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM bars WHERE timeframe = :tf AND ts < COALESCE(("
                "  SELECT ts FROM bars b2 WHERE b2.timeframe = :tf AND b2.symbol = bars.symbol"
                "  ORDER BY ts DESC LIMIT 1 OFFSET :off), '')",
                {"tf": timeframe, "off": keep - 1})

    def latest_ts(self, timeframe: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT MAX(ts) m FROM bars WHERE timeframe = ?",
                            (timeframe,)).fetchone()
            return row["m"]

    def version(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT v FROM meta WHERE k = 'bar_version'").fetchone()
            return int(row["v"]) if row else 0

    # -- instruments -----------------------------------------------------
    def upsert_instruments(self, rows: Iterable[Dict[str, Any]]) -> None:
        now = _now()
        with self._conn() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO instruments (symbol,yf_symbol,name,sector,industry,"
                    " market_cap,index_memberships,fno,fundamentals_json,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET yf_symbol=excluded.yf_symbol,"
                    " name=excluded.name,"
                    " sector=COALESCE(excluded.sector, instruments.sector),"
                    " industry=COALESCE(excluded.industry, instruments.industry),"
                    " market_cap=COALESCE(excluded.market_cap, instruments.market_cap),"
                    " index_memberships=excluded.index_memberships, fno=excluded.fno,"
                    " fundamentals_json=CASE WHEN excluded.fundamentals_json='{}'"
                    "   THEN instruments.fundamentals_json ELSE excluded.fundamentals_json END,"
                    " updated_at=excluded.updated_at",
                    (r["symbol"], r["yf_symbol"], r.get("name", ""), r.get("sector"),
                     r.get("industry"), r.get("market_cap"),
                     json.dumps(r.get("index_memberships", [])), int(r.get("fno", False)),
                     json.dumps(r.get("fundamentals", {})), now))

    def instruments_df(self) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql_query("SELECT * FROM instruments", c)
        if df.empty:
            return df.set_index(pd.Index([], name="symbol")) if "symbol" not in df.index.names else df
        df["index_memberships"] = df["index_memberships"].map(json.loads)
        df["fundamentals"] = df["fundamentals_json"].map(json.loads)
        df["fno"] = df["fno"].astype(bool)
        return df.drop(columns=["fundamentals_json"]).set_index("symbol")

    # -- scanners ----------------------------------------------------------
    def create_scanner(self, user_id: str, name: str, description: str,
                       definition: Dict[str, Any]) -> str:
        sid = uuid.uuid4().hex
        now = _now()
        with self._conn() as c:
            c.execute("INSERT INTO scanners VALUES (?,?,?,?,?,?,?)",
                      (sid, user_id, name, description, json.dumps(definition), now, now))
        return sid

    def upsert_prebuilt(self, name: str, description: str,
                        definition: Dict[str, Any]) -> None:
        now = _now()
        with self._conn() as c:
            row = c.execute("SELECT id FROM scanners WHERE user_id IS NULL AND name = ?",
                            (name,)).fetchone()
            if row:
                c.execute("UPDATE scanners SET description=?, definition_json=?, updated_at=?"
                          " WHERE id=?", (description, json.dumps(definition), now, row["id"]))
            else:
                c.execute("INSERT INTO scanners VALUES (?,NULL,?,?,?,?,?)",
                          (uuid.uuid4().hex, name, description, json.dumps(definition), now, now))

    def get_scanner(self, sid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scanners WHERE id = ?", (sid,)).fetchone()
        return self._scanner_dict(row) if row else None

    def list_scanners(self, user_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM scanners WHERE user_id IS NULL OR user_id = ? "
                "ORDER BY user_id IS NOT NULL, name", (user_id,)).fetchall()
        return [self._scanner_dict(r) for r in rows]

    def update_scanner(self, sid: str, name: str, description: str,
                       definition: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("UPDATE scanners SET name=?, description=?, definition_json=?,"
                      " updated_at=? WHERE id=?",
                      (name, description, json.dumps(definition), _now(), sid))

    def delete_scanner(self, sid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM scanners WHERE id = ?", (sid,))

    @staticmethod
    def _scanner_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json"))
        d["prebuilt"] = d["user_id"] is None
        return d


_store: Optional[ScannerStore] = None
_lock = threading.Lock()


def get_scanner_store() -> ScannerStore:
    global _store
    with _lock:
        if _store is None:
            _store = ScannerStore(os.environ.get("SCANNER_DB_PATH", "data/scanner.sqlite"))
        return _store


def reset_scanner_store_for_tests(path: Path | str) -> ScannerStore:
    global _store
    with _lock:
        _store = ScannerStore(path)
        return _store
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scanner_store.py -v` — Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/ tests/scanner_utils.py tests/test_scanner_store.py
git commit -m "feat(scanner): SQLite store for instruments, bars, scanner definitions"
```

---

### Task 2: AST schema

**Files:**
- Create: `apps/api/scanner/schema.py`
- Test: `tests/test_scanner_schema.py`

**Interfaces:**
- Produces:
  - `Operand`, `Condition`, `Group` Pydantic models (shapes below — engine and routes consume these)
  - `parse_definition(data: dict) -> Group` — raises `DefinitionError(msg)` on limit violations, `pydantic.ValidationError` on shape errors
  - `DefinitionError(ValueError)`
  - constants: `FIELDS`, `FUNCTIONS`, `PATTERNS`, `FUNDAMENTALS`, `METAS`, `EXPR_OPS`, `TIMEFRAMES`, `MAX_NODES=50`, `MAX_DEPTH=8`, `MAX_PERIOD=500`, `MAX_JSON_BYTES=32768`
  - `DEFINITION_JSON_SCHEMA: dict` (for the NL structured-output prompt)

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_schema.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.scanner.schema import (
    DefinitionError, Group, MAX_NODES, parse_definition,
)

GOLDEN_CROSS = {"logic": "AND", "children": [
    {"timeframe": "1d",
     "left": {"fn": "SMA", "of": "close", "period": 50},
     "op": "crosses_above",
     "right": {"fn": "SMA", "of": "close", "period": 200}},
]}


def cond(**kw):
    base = {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}
    base.update(kw)
    return base


def test_valid_definition_parses():
    g = parse_definition(GOLDEN_CROSS)
    assert isinstance(g, Group)
    assert g.children[0].left.fn == "SMA"


def test_nested_groups_and_expr():
    d = {"logic": "OR", "children": [
        {"logic": "AND", "children": [
            cond(right={"expr": "*", "args": [{"const": 2},
                 {"fn": "SMA", "of": "volume", "period": 20}]}),
            {"timeframe": "1d", "left": {"pattern": "bullish_engulfing"}},
        ]},
        cond(left={"fundamental": "market_cap"}, right={"const": 1000}),
    ]}
    parse_definition(d)


def test_operand_must_have_exactly_one_kind():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"field": "close", "const": 5})]})
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [cond(left={})]})


def test_pattern_condition_takes_no_op():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"pattern": "doji"}, "op": ">", "right": {"const": 1}}]})


def test_unknown_names_rejected():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [cond(left={"field": "closse"})]})
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"fn": "SUPERDUPER", "period": 5})]})


def test_period_cap():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"fn": "SMA", "of": "close", "period": 501})]})


def test_node_count_limit():
    d = {"logic": "AND", "children": [cond() for _ in range(MAX_NODES + 1)]}
    with pytest.raises(DefinitionError):
        parse_definition(d)


def test_depth_limit():
    d = cond()
    for _ in range(9):
        d = {"logic": "AND", "children": [d]}
    with pytest.raises(DefinitionError):
        parse_definition(d)


def test_size_limit():
    d = {"logic": "AND", "children": [cond() for _ in range(40)]}
    d["children"][0]["left"] = {"field": "close"}
    big = {"logic": "AND", "children": [dict(cond(), note="x" * 40000)]}
    with pytest.raises(DefinitionError):
        parse_definition(big)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scanner_schema.py -v`
Expected: FAIL — `ImportError` (schema module missing)

- [ ] **Step 3: Implement the schema**

`apps/api/scanner/schema.py`:

```python
"""Scanner definition AST — the one schema shared by builder UI, NL generator,
prebuilt seeds, storage, and the engine."""
from __future__ import annotations

import json
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMEFRAMES = ("5m", "15m", "1h", "1d", "1w", "1mo")
Timeframe = Literal["5m", "15m", "1h", "1d", "1w", "1mo"]

FIELDS = {"open", "high", "low", "close", "volume", "vwap", "typical_price",
          "gap_pct", "change_pct", "body", "upper_wick", "lower_wick"}
FUNCTIONS = {"SMA", "EMA", "WMA", "HMA", "VWMA", "RSI", "STOCH", "STOCHRSI", "CCI",
             "WILLR", "ROC", "MOM", "MACD", "ADX", "SUPERTREND", "PSAR", "ATR",
             "BBANDS", "BBWIDTH", "STDDEV", "OBV", "MFI", "CMF",
             "HIGHEST", "LOWEST", "SUM", "AVG", "COUNT"}
#: Functions that read OHLCV directly and ignore `of`.
OHLC_FUNCTIONS = {"STOCH", "CCI", "WILLR", "ADX", "SUPERTREND", "PSAR", "ATR",
                  "OBV", "MFI", "CMF"}
#: Functions with defaulted params (no `period` required).
NO_PERIOD_OK = {"MACD", "PSAR", "OBV", "SUPERTREND"}
PATTERNS = {"doji", "hammer", "inverted_hammer", "shooting_star", "hanging_man",
            "bullish_engulfing", "bearish_engulfing", "morning_star", "evening_star",
            "three_white_soldiers", "three_black_crows", "piercing", "dark_cloud_cover"}
FUNDAMENTALS = {"market_cap", "pe", "pb", "roe", "dividend_yield", "eps",
                "debt_to_equity", "revenue_growth"}
METAS = {"sector", "industry", "index", "fno"}
EXPR_OPS = {"+", "-", "*", "/", "abs", "min", "max"}

MAX_PERIOD = 500
MAX_NODES = 50
MAX_DEPTH = 8
MAX_JSON_BYTES = 32768


class DefinitionError(ValueError):
    """Definition breaks a hard limit (size / node count / depth)."""


class Operand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    const: Optional[float] = None
    const_str: Optional[str] = None
    const_list: Optional[List[str]] = None
    field: Optional[str] = None
    fn: Optional[str] = None
    of: Optional[Union[str, "Operand"]] = None   # base series for fn (default close)
    period: Optional[int] = Field(None, ge=1, le=MAX_PERIOD)
    params: Dict[str, float] = Field(default_factory=dict)  # e.g. MACD fast/slow/signal, BBANDS std
    component: Optional[str] = None              # MACD line|signal|hist, BBANDS upper|mid|lower, STOCH k|d, ADX adx|pdi|mdi
    expr: Optional[str] = None
    args: Optional[List["Operand"]] = None
    fundamental: Optional[str] = None
    meta: Optional[str] = None
    pattern: Optional[str] = None
    cond: Optional["Condition"] = None           # COUNT only
    bars_ago: int = Field(0, ge=0, le=MAX_PERIOD)

    @model_validator(mode="after")
    def _check(self) -> "Operand":
        kinds = [k for k in ("const", "const_str", "const_list", "field", "fn",
                             "expr", "fundamental", "meta", "pattern")
                 if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"operand must set exactly one kind, got {kinds or 'none'}")
        if self.field is not None and self.field not in FIELDS:
            raise ValueError(f"unknown field {self.field!r}")
        if self.fn is not None:
            if self.fn not in FUNCTIONS:
                raise ValueError(f"unknown function {self.fn!r}")
            if self.fn == "COUNT":
                if self.cond is None or self.period is None:
                    raise ValueError("COUNT needs cond and period")
            elif self.period is None and self.fn not in NO_PERIOD_OK:
                raise ValueError(f"{self.fn} needs period")
            if isinstance(self.of, str) and self.of not in FIELDS:
                raise ValueError(f"unknown base field {self.of!r}")
        if self.expr is not None and (self.expr not in EXPR_OPS or not self.args):
            raise ValueError("expr needs a known operator and args")
        if self.fundamental is not None and self.fundamental not in FUNDAMENTALS:
            raise ValueError(f"unknown fundamental {self.fundamental!r}")
        if self.meta is not None and self.meta not in METAS:
            raise ValueError(f"unknown meta {self.meta!r}")
        if self.pattern is not None and self.pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {self.pattern!r}")
        return self


Op = Literal[">", "<", ">=", "<=", "==", "!=", "in", "crosses_above", "crosses_below"]


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeframe: Timeframe = "1d"
    left: Operand
    op: Optional[Op] = None
    right: Optional[Operand] = None
    for_n_bars: Optional[int] = Field(None, ge=1, le=100)

    @model_validator(mode="after")
    def _shape(self) -> "Condition":
        if self.left.pattern is not None:
            if self.op is not None or self.right is not None:
                raise ValueError("pattern condition takes no op/right")
        elif self.op is None or self.right is None:
            raise ValueError("condition needs op and right")
        return self


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logic: Literal["AND", "OR"]
    children: List[Union["Group", Condition]] = Field(min_length=1)


Operand.model_rebuild()
Condition.model_rebuild()
Group.model_rebuild()

DEFINITION_JSON_SCHEMA = Group.model_json_schema()


def _walk(node: Union[Group, Condition], depth: int) -> tuple[int, int]:
    """(condition count, max depth) under node."""
    if isinstance(node, Condition):
        return 1, depth
    total, deepest = 0, depth
    for child in node.children:
        n, d = _walk(child, depth + 1)
        total += n
        deepest = max(deepest, d)
    return total, deepest


def parse_definition(data: dict) -> Group:
    raw = json.dumps(data)
    if len(raw.encode()) > MAX_JSON_BYTES:
        raise DefinitionError(f"definition exceeds {MAX_JSON_BYTES} bytes")
    group = Group.model_validate(data)
    nodes, depth = _walk(group, 1)
    if nodes > MAX_NODES:
        raise DefinitionError(f"too many conditions ({nodes} > {MAX_NODES})")
    if depth > MAX_DEPTH:
        raise DefinitionError(f"nesting too deep ({depth} > {MAX_DEPTH})")
    return group
```

Note: `test_size_limit` sends an unknown key `note` — `extra="forbid"` would reject it, but the **size check runs first** (before `model_validate`), so `DefinitionError` is what surfaces. That ordering is the point of the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scanner_schema.py -v` — Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/schema.py tests/test_scanner_schema.py
git commit -m "feat(scanner): pydantic AST schema with hard limits"
```

---

### Task 3: NSE market calendar

**Files:**
- Create: `apps/api/scanner/calendar.py`
- Test: `tests/test_scanner_calendar.py`

**Interfaces:**
- Produces: `IST` (ZoneInfo), `is_trading_day(d: date) -> bool`, `is_market_open(now: datetime | None = None) -> bool` (IST 09:15–15:30 on trading days), `HOLIDAYS: set[str]` (ISO dates)

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_calendar.py`:

```python
from __future__ import annotations

from datetime import date, datetime

from apps.api.scanner.calendar import IST, is_market_open, is_trading_day


def test_weekend_closed():
    assert not is_trading_day(date(2026, 8, 15))  # Saturday (also Independence Day)
    assert not is_trading_day(date(2026, 8, 16))  # Sunday


def test_holiday_closed():
    assert not is_trading_day(date(2026, 1, 26))  # Republic Day


def test_weekday_open_hours():
    assert is_trading_day(date(2026, 8, 13))  # Thursday
    assert is_market_open(datetime(2026, 8, 13, 10, 0, tzinfo=IST))
    assert not is_market_open(datetime(2026, 8, 13, 9, 0, tzinfo=IST))
    assert not is_market_open(datetime(2026, 8, 13, 15, 45, tzinfo=IST))
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/test_scanner_calendar.py -v` → ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/calendar.py`:

```python
"""NSE market calendar: fixed hours + a static holiday list.

ponytail: static holiday list, refreshed by hand each January from NSE's
published circular. A wrong entry costs one skipped/extra ingest cycle, not
data corruption — upgrade to an exchange-calendar library only if that ever
actually bites.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE trading holidays 2026. Fixed national dates are certain; VERIFY the
# movable ones (Holi, Eid, Diwali, etc.) against NSE's 2026 circular at
# https://www.nseindia.com/resources/exchange-communication-holidays
HOLIDAYS: set[str] = {
    "2026-01-26",  # Republic Day
    "2026-03-04",  # Holi (verify)
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day (Saturday in 2026)
    "2026-10-02",  # Gandhi Jayanti
    "2026-11-09",  # Diwali (verify)
    "2026-12-25",  # Christmas
}

OPEN = time(9, 15)
CLOSE = time(15, 30)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def is_market_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(IST)).astimezone(IST)
    return is_trading_day(now.date()) and OPEN <= now.time() <= CLOSE
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_calendar.py -v` → 3 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/calendar.py tests/test_scanner_calendar.py
git commit -m "feat(scanner): NSE market calendar"
```

---

### Task 4: Indicators — Panel + operand evaluation

**Files:**
- Create: `apps/api/scanner/indicators.py`
- Test: `tests/test_scanner_indicators.py`

**Interfaces:**
- Consumes: `Operand` from `apps.api.scanner.schema`
- Produces:
  - `Panel` dataclass: `open/high/low/close/volume: pd.DataFrame` (index `DatetimeIndex`, columns symbols), `fundamentals: pd.DataFrame` (index symbols, numeric cols per `FUNDAMENTALS`), `meta: pd.DataFrame` (index symbols; cols `sector`, `industry`, `index_memberships` (list), `fno` (bool))
  - `eval_operand(op: Operand, panel: Panel, cond_eval=None) -> pd.DataFrame | pd.Series | float | str | list` — DataFrame (time×symbol) for numeric operands; `pd.Series` of strings/lists for `meta`; scalar for consts. `cond_eval(cond) -> pd.DataFrame[bool]` is injected by the engine for `COUNT`.
  - `describe(op: Operand) -> str` — human label, e.g. `EMA(20)`, `2*SMA(volume,20)`

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_indicators.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from apps.api.scanner.indicators import Panel, describe, eval_operand
from apps.api.scanner.schema import Operand


def panel_from_closes(closes: dict[str, list[float]], volumes=None) -> Panel:
    idx = pd.date_range("2026-01-05", periods=len(next(iter(closes.values()))), freq="D")
    c = pd.DataFrame(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c)
    v = pd.DataFrame(volumes, index=idx, dtype=float) if volumes else c * 0 + 1000
    return Panel(open=o, high=np.maximum(o, c) * 1.001, low=np.minimum(o, c) * 0.999,
                 close=c, volume=v,
                 fundamentals=pd.DataFrame({"market_cap": {s: 5000.0 for s in closes}}),
                 meta=pd.DataFrame({"sector": {s: "IT" for s in closes}}))


def test_sma_and_field():
    p = panel_from_closes({"A": [1, 2, 3, 4, 5]})
    sma = eval_operand(Operand(fn="SMA", of="close", period=3), p)
    assert sma["A"].iloc[-1] == pytest.approx(4.0)
    close = eval_operand(Operand(field="close"), p)
    assert close["A"].iloc[-1] == 5


def test_bars_ago_shifts():
    p = panel_from_closes({"A": [1, 2, 3, 4, 5]})
    prev = eval_operand(Operand(field="close", bars_ago=2), p)
    assert prev["A"].iloc[-1] == 3


def test_rsi_extremes():
    p = panel_from_closes({"UP": list(range(1, 31)), "DN": list(range(60, 30, -1))})
    rsi = eval_operand(Operand(fn="RSI", of="close", period=14), p)
    assert rsi["UP"].iloc[-1] > 99
    assert rsi["DN"].iloc[-1] < 1


def test_expr_arithmetic():
    p = panel_from_closes({"A": [10, 10, 10, 10, 20]})
    op = Operand(expr="*", args=[Operand(const=2), Operand(field="close")])
    assert eval_operand(op, p)["A"].iloc[-1] == 40


def test_macd_components_differ():
    p = panel_from_closes({"A": [float(i) + (i % 3) for i in range(60)]})
    line = eval_operand(Operand(fn="MACD"), p)
    sig = eval_operand(Operand(fn="MACD", component="signal"), p)
    assert not np.isclose(line["A"].iloc[-1], sig["A"].iloc[-1])


def test_rolling_highest():
    p = panel_from_closes({"A": [1, 9, 2, 3, 4]})
    hh = eval_operand(Operand(fn="HIGHEST", of="close", period=3), p)
    assert hh["A"].iloc[-1] == 4
    hh5 = eval_operand(Operand(fn="HIGHEST", of="close", period=5), p)
    assert hh5["A"].iloc[-1] == 9


def test_fundamental_broadcasts():
    p = panel_from_closes({"A": [1, 2, 3]})
    mc = eval_operand(Operand(fundamental="market_cap"), p)
    assert mc.shape == p.close.shape
    assert (mc["A"] == 5000.0).all()


def test_supertrend_below_close_in_uptrend():
    p = panel_from_closes({"A": [float(100 + i) for i in range(60)]})
    st = eval_operand(Operand(fn="SUPERTREND"), p)
    assert st["A"].iloc[-1] < p.close["A"].iloc[-1]


def test_describe():
    assert describe(Operand(fn="EMA", of="close", period=20)) == "EMA(20)"
    assert describe(Operand(expr="*", args=[Operand(const=2),
                    Operand(fn="SMA", of="volume", period=20)])) == "2*SMA(volume,20)"
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/test_scanner_indicators.py -v` → ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/indicators.py`:

```python
"""Operand evaluation on wide frames (time x symbols).

Everything vectorizes across the whole universe at once; the two recursive
indicators (SUPERTREND, PSAR) loop over time but stay vectorized across
symbols, so a full-universe scan stays interactive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd

from apps.api.scanner.schema import Operand


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    fundamentals: pd.DataFrame  # index: symbol
    meta: pd.DataFrame          # index: symbol


Result = Union[pd.DataFrame, pd.Series, float, str, list]


def eval_operand(op: Operand, panel: Panel,
                 cond_eval: Optional[Callable] = None) -> Result:
    res = _eval(op, panel, cond_eval)
    if op.bars_ago and isinstance(res, pd.DataFrame):
        res = res.shift(op.bars_ago)
    return res


def _eval(op: Operand, p: Panel, cond_eval) -> Result:
    if op.const is not None:
        return float(op.const)
    if op.const_str is not None:
        return op.const_str
    if op.const_list is not None:
        return list(op.const_list)
    if op.field is not None:
        return _field(op.field, p)
    if op.fundamental is not None:
        col = p.fundamentals.get(op.fundamental)
        if col is None:
            col = pd.Series(np.nan, index=p.close.columns)
        # Broadcast the per-symbol value over time so it composes like any frame.
        return pd.DataFrame(np.tile(col.reindex(p.close.columns).to_numpy(),
                                    (len(p.close.index), 1)),
                            index=p.close.index, columns=p.close.columns)
    if op.meta is not None:
        return p.meta[op.meta] if op.meta in p.meta else pd.Series(index=p.meta.index, dtype=object)
    if op.pattern is not None:
        from apps.api.scanner.patterns import pattern_frame
        return pattern_frame(op.pattern, p)
    if op.expr is not None:
        return _expr(op, p, cond_eval)
    if op.fn is not None:
        return _fn(op, p, cond_eval)
    raise ValueError("empty operand")  # unreachable: schema validated


def _base(op: Operand, p: Panel, cond_eval) -> pd.DataFrame:
    if op.of is None or op.of == "close":
        return p.close
    if isinstance(op.of, str):
        return _field(op.of, p)
    return _eval(op.of, p, cond_eval)


def _field(name: str, p: Panel) -> pd.DataFrame:
    if name in ("open", "high", "low", "close", "volume"):
        return getattr(p, name)
    if name == "typical_price":
        return (p.high + p.low + p.close) / 3
    if name == "vwap":
        # Session-anchored: cumulative typical-price*volume per calendar day.
        tp = (p.high + p.low + p.close) / 3
        day = np.array([d.date() for d in p.close.index])
        num = (tp * p.volume).groupby(day).cumsum()
        den = p.volume.groupby(day).cumsum()
        return num / den
    if name == "change_pct":
        return p.close.pct_change() * 100
    if name == "gap_pct":
        return (p.open - p.close.shift(1)) / p.close.shift(1) * 100
    if name == "body":
        return (p.close - p.open).abs()
    if name == "upper_wick":
        return p.high - np.maximum(p.open, p.close)
    if name == "lower_wick":
        return np.minimum(p.open, p.close) - p.low
    raise ValueError(f"unknown field {name}")


def _expr(op: Operand, p: Panel, cond_eval) -> Result:
    vals = [_shifted(a, p, cond_eval) for a in op.args]
    if op.expr == "abs":
        return vals[0].abs() if isinstance(vals[0], pd.DataFrame) else abs(vals[0])
    if op.expr in ("min", "max"):
        f = np.minimum if op.expr == "min" else np.maximum
        out = vals[0]
        for v in vals[1:]:
            out = f(out, v)
        return out
    out = vals[0]
    for v in vals[1:]:
        out = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
               "*": lambda a, b: a * b, "/": lambda a, b: a / b}[op.expr](out, v)
    return out


def _shifted(op: Operand, p: Panel, cond_eval) -> Result:
    return eval_operand(op, p, cond_eval)


def _wilder(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(alpha=1 / n, adjust=False).mean()


def _sma(x, n):
    return x.rolling(n).mean()


def _ema(x, n):
    return x.ewm(span=n, adjust=False).mean()


def _wma(x: pd.DataFrame, n: int) -> pd.DataFrame:
    w = np.arange(1, n + 1, dtype=float)
    # ponytail: rolling.apply is a per-column python loop — fine at 320 rows,
    # cached by the engine; swap for a strided dot product if profiling says so.
    return x.rolling(n).apply(lambda a: np.dot(a, w) / w.sum(), raw=True)


def _true_range(p: Panel) -> pd.DataFrame:
    pc = p.close.shift(1)
    return np.maximum(p.high - p.low,
                      np.maximum((p.high - pc).abs(), (p.low - pc).abs()))


def _atr(p: Panel, n: int) -> pd.DataFrame:
    return _wilder(_true_range(p), n)


def _rsi(x: pd.DataFrame, n: int) -> pd.DataFrame:
    d = x.diff()
    up = _wilder(d.clip(lower=0), n)
    dn = _wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn)


def _stoch_k(p: Panel, n: int) -> pd.DataFrame:
    ll = p.low.rolling(n).min()
    hh = p.high.rolling(n).max()
    return 100 * (p.close - ll) / (hh - ll)


def _bbands(x: pd.DataFrame, n: int, k: float):
    mid = _sma(x, n)
    sd = x.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def _adx(p: Panel, n: int):
    up = p.high.diff()
    dn = -p.low.diff()
    plus = up.where((up > dn) & (up > 0), 0.0)
    minus = dn.where((dn > up) & (dn > 0), 0.0)
    atr = _atr(p, n)
    pdi = 100 * _wilder(plus, n) / atr
    mdi = 100 * _wilder(minus, n) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return _wilder(dx, n), pdi, mdi


def _supertrend(p: Panel, n: int, mult: float) -> pd.DataFrame:
    atr = _atr(p, n)
    hl2 = (p.high + p.low) / 2
    ub = (hl2 + mult * atr).to_numpy()
    lb = (hl2 - mult * atr).to_numpy()
    close = p.close.to_numpy()
    rows, cols = close.shape
    fub = np.array(ub, copy=True)
    flb = np.array(lb, copy=True)
    st = np.full_like(close, np.nan)
    dirn = np.ones(cols)
    for i in range(1, rows):
        fub[i] = np.where((ub[i] < fub[i - 1]) | (close[i - 1] > fub[i - 1]), ub[i], fub[i - 1])
        flb[i] = np.where((lb[i] > flb[i - 1]) | (close[i - 1] < flb[i - 1]), lb[i], flb[i - 1])
        dirn = np.where(close[i] > fub[i - 1], 1.0,
                        np.where(close[i] < flb[i - 1], -1.0, dirn))
        st[i] = np.where(dirn > 0, flb[i], fub[i])
    return pd.DataFrame(st, index=p.close.index, columns=p.close.columns)


def _psar(p: Panel, step: float, cap: float) -> pd.DataFrame:
    # ponytail: standard SAR without the two-bar clamp refinement — matches
    # direction/level closely enough for scan conditions; refine if users compare
    # against TradingView values and care about the last decimal.
    h = p.high.to_numpy()
    l = p.low.to_numpy()
    rows, cols = h.shape
    out = np.full_like(h, np.nan)
    bull = np.ones(cols, dtype=bool)
    af = np.full(cols, step)
    ep = h[0].copy()
    out[0] = l[0]
    for i in range(1, rows):
        sar = out[i - 1] + af * (ep - out[i - 1])
        rev_to_bear = bull & (l[i] < sar)
        rev_to_bull = ~bull & (h[i] > sar)
        sar = np.where(rev_to_bear | rev_to_bull, ep, sar)
        new_bull = np.where(rev_to_bear, False, np.where(rev_to_bull, True, bull)).astype(bool)
        af = np.where(rev_to_bear | rev_to_bull, step, af)
        ep = np.where(rev_to_bear, l[i], np.where(rev_to_bull, h[i], ep))
        hi_ext = new_bull & (h[i] > ep)
        lo_ext = ~new_bull & (l[i] < ep)
        af = np.where(hi_ext | lo_ext, np.minimum(af + step, cap), af)
        ep = np.where(hi_ext, h[i], np.where(lo_ext, l[i], ep))
        bull = new_bull
        out[i] = sar
    return pd.DataFrame(out, index=p.close.index, columns=p.close.columns)


def _fn(op: Operand, p: Panel, cond_eval) -> pd.DataFrame:
    n = op.period
    x = _base(op, p, cond_eval)
    name = op.fn
    if name == "SMA":
        return _sma(x, n)
    if name == "EMA":
        return _ema(x, n)
    if name == "WMA":
        return _wma(x, n)
    if name == "HMA":
        return _wma(2 * _wma(x, max(n // 2, 1)) - _wma(x, n), max(int(np.sqrt(n)), 1))
    if name == "VWMA":
        return (x * p.volume).rolling(n).sum() / p.volume.rolling(n).sum()
    if name == "RSI":
        return _rsi(x, n)
    if name == "STOCHRSI":
        r = _rsi(x, n)
        lo, hi = r.rolling(n).min(), r.rolling(n).max()
        return 100 * (r - lo) / (hi - lo)
    if name == "STOCH":
        k = _stoch_k(p, n)
        return _sma(k, int(op.params.get("d", 3))) if op.component == "d" else k
    if name == "CCI":
        tp = (p.high + p.low + p.close) / 3
        mad = tp.rolling(n).apply(lambda a: np.abs(a - a.mean()).mean(), raw=True)
        return (tp - _sma(tp, n)) / (0.015 * mad)
    if name == "WILLR":
        hh, ll = p.high.rolling(n).max(), p.low.rolling(n).min()
        return -100 * (hh - p.close) / (hh - ll)
    if name == "ROC":
        return x.pct_change(n) * 100
    if name == "MOM":
        return x.diff(n)
    if name == "MACD":
        fast = int(op.params.get("fast", 12))
        slow = int(op.params.get("slow", 26))
        sig = int(op.params.get("signal", 9))
        line = _ema(x, fast) - _ema(x, slow)
        if op.component == "signal":
            return _ema(line, sig)
        if op.component == "hist":
            return line - _ema(line, sig)
        return line
    if name == "ADX":
        adx, pdi, mdi = _adx(p, n or 14)
        return {"pdi": pdi, "mdi": mdi}.get(op.component, adx)
    if name == "SUPERTREND":
        return _supertrend(p, n or 10, op.params.get("mult", 3.0))
    if name == "PSAR":
        return _psar(p, op.params.get("step", 0.02), op.params.get("cap", 0.2))
    if name == "ATR":
        return _atr(p, n)
    if name == "BBANDS":
        u, m, l = _bbands(x, n, op.params.get("std", 2.0))
        return {"upper": u, "lower": l}.get(op.component or "upper", m) \
            if op.component in ("mid",) else {"upper": u, "mid": m, "lower": l}.get(op.component or "upper")
    if name == "BBWIDTH":
        u, m, l = _bbands(x, n, op.params.get("std", 2.0))
        return (u - l) / m * 100
    if name == "STDDEV":
        return x.rolling(n).std()
    if name == "OBV":
        return (np.sign(p.close.diff()).fillna(0) * p.volume).cumsum()
    if name == "MFI":
        tp = (p.high + p.low + p.close) / 3
        mf = tp * p.volume
        pos = mf.where(tp > tp.shift(1), 0.0).rolling(n).sum()
        neg = mf.where(tp < tp.shift(1), 0.0).rolling(n).sum()
        return 100 - 100 / (1 + pos / neg)
    if name == "CMF":
        rng = (p.high - p.low).replace(0, np.nan)
        mfv = ((p.close - p.low) - (p.high - p.close)) / rng * p.volume
        return mfv.rolling(n).sum() / p.volume.rolling(n).sum()
    if name == "HIGHEST":
        return x.rolling(n).max()
    if name == "LOWEST":
        return x.rolling(n).min()
    if name == "SUM":
        return x.rolling(n).sum()
    if name == "AVG":
        return x.rolling(n).mean()
    if name == "COUNT":
        if cond_eval is None:
            raise ValueError("COUNT needs engine context")
        return cond_eval(op.cond).astype(float).rolling(n).sum()
    raise ValueError(f"unknown function {name}")  # unreachable: schema validated


def describe(op: Operand) -> str:
    if op.const is not None:
        return f"{op.const:g}"
    if op.const_str is not None:
        return op.const_str
    if op.const_list is not None:
        return ",".join(op.const_list)
    if op.field is not None:
        base = op.field
    elif op.fundamental is not None:
        base = op.fundamental
    elif op.meta is not None:
        base = op.meta
    elif op.pattern is not None:
        base = op.pattern
    elif op.expr is not None:
        if op.expr in ("abs", "min", "max"):
            base = f"{op.expr}({','.join(describe(a) for a in op.args)})"
        else:
            base = op.expr.join(describe(a) for a in op.args)
    else:
        of = "" if op.of is None or op.of == "close" else \
            (op.of if isinstance(op.of, str) else describe(op.of)) + ","
        parts = f"{of}{op.period}" if op.period else of.rstrip(",")
        comp = f".{op.component}" if op.component else ""
        base = f"{op.fn}({parts}){comp}"
    if op.bars_ago:
        base += f"[{op.bars_ago} ago]"
    return base
```

Fix the BBANDS branch while implementing — the conditional above is convoluted; write it plainly:

```python
    if name == "BBANDS":
        u, m, l = _bbands(x, n, op.params.get("std", 2.0))
        return {"upper": u, "mid": m, "lower": l}[op.component or "upper"]
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_indicators.py -v` → 9 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/indicators.py tests/test_scanner_indicators.py
git commit -m "feat(scanner): wide-frame operand evaluation (fields, indicators, expressions)"
```

---

### Task 5: Candlestick patterns

**Files:**
- Create: `apps/api/scanner/patterns.py`
- Test: `tests/test_scanner_patterns.py`

**Interfaces:**
- Consumes: `Panel` from `apps.api.scanner.indicators`
- Produces: `pattern_frame(name: str, panel: Panel) -> pd.DataFrame` (bool, time×symbol)

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_patterns.py`:

```python
from __future__ import annotations

import pandas as pd

from apps.api.scanner.indicators import Panel
from apps.api.scanner.patterns import pattern_frame


def panel_ohlc(rows: list[tuple[float, float, float, float]]) -> Panel:
    """rows = [(open, high, low, close), ...] for one symbol 'A'."""
    idx = pd.date_range("2026-01-05", periods=len(rows), freq="D")
    o, h, l, c = (pd.DataFrame({"A": [r[i] for r in rows]}, index=idx, dtype=float)
                  for i in range(4))
    return Panel(open=o, high=h, low=l, close=c, volume=c * 0 + 1000,
                 fundamentals=pd.DataFrame(index=["A"]), meta=pd.DataFrame(index=["A"]))


def test_doji():
    p = panel_ohlc([(100, 101, 99, 100.05)])
    assert pattern_frame("doji", p)["A"].iloc[-1]
    p2 = panel_ohlc([(100, 105, 99, 105)])
    assert not pattern_frame("doji", p2)["A"].iloc[-1]


def test_hammer():
    # Long lower shadow, small body at the top, tiny upper shadow.
    p = panel_ohlc([(100, 100.6, 95, 100.5)])
    assert pattern_frame("hammer", p)["A"].iloc[-1]


def test_bullish_engulfing():
    p = panel_ohlc([(102, 102.5, 99.5, 100),    # red
                    (99.8, 103, 99.5, 102.5)])  # green engulfing prior body
    assert pattern_frame("bullish_engulfing", p)["A"].iloc[-1]
    assert not pattern_frame("bearish_engulfing", p)["A"].iloc[-1]


def test_three_white_soldiers():
    p = panel_ohlc([(100, 102.2, 99.8, 102), (101, 104.2, 100.8, 104),
                    (103, 106.2, 102.8, 106)])
    assert pattern_frame("three_white_soldiers", p)["A"].iloc[-1]


def test_piercing():
    p = panel_ohlc([(104, 104.5, 99.5, 100),      # red
                    (99, 103.5, 98.8, 102.5)])    # opens below prior close, closes above midpoint
    assert pattern_frame("piercing", p)["A"].iloc[-1]
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/patterns.py`:

```python
"""Vectorized candlestick pattern rules on wide OHLC frames.

Textbook geometric definitions. Trend-context patterns (shooting star,
hanging man, stars) use a 3-bars-back close comparison as the trend proxy —
ponytail: crude but cheap; swap for an SMA-slope filter if false positives bug users.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from apps.api.scanner.indicators import Panel


def pattern_frame(name: str, p: Panel) -> pd.DataFrame:
    o, h, l, c = p.open, p.high, p.low, p.close
    body = c - o
    ab = body.abs()
    rng = (h - l).replace(0, np.nan)
    upsh = h - np.maximum(o, c)
    losh = np.minimum(o, c) - l
    green = c > o
    red = o > c
    o1, c1, ab1 = o.shift(1), c.shift(1), ab.shift(1)
    green1, red1 = green.shift(1, fill_value=False), red.shift(1, fill_value=False)
    uptrend = c.shift(1) > c.shift(3)
    downtrend = c.shift(1) < c.shift(3)

    if name == "doji":
        out = ab <= 0.1 * rng
    elif name == "hammer":
        out = (losh >= 2 * ab) & (upsh <= ab) & (ab > 0)
    elif name == "inverted_hammer":
        out = (upsh >= 2 * ab) & (losh <= ab) & (ab > 0)
    elif name == "shooting_star":
        out = (upsh >= 2 * ab) & (losh <= ab) & (ab > 0) & uptrend
    elif name == "hanging_man":
        out = (losh >= 2 * ab) & (upsh <= ab) & (ab > 0) & uptrend
    elif name == "bullish_engulfing":
        out = red1 & green & (o <= c1) & (c >= o1)
    elif name == "bearish_engulfing":
        out = green1 & red & (o >= c1) & (c <= o1)
    elif name == "morning_star":
        mid2 = (o.shift(2) + c.shift(2)) / 2
        out = (red.shift(2, fill_value=False) & (ab1 <= 0.3 * ab.shift(2))
               & green & (c > mid2) & downtrend.shift(1, fill_value=False))
    elif name == "evening_star":
        mid2 = (o.shift(2) + c.shift(2)) / 2
        out = (green.shift(2, fill_value=False) & (ab1 <= 0.3 * ab.shift(2))
               & red & (c < mid2) & uptrend.shift(1, fill_value=False))
    elif name == "three_white_soldiers":
        out = (green & green1 & green.shift(2, fill_value=False)
               & (c > c1) & (c1 > c.shift(2))
               & (o > o1) & (o < c1) & (o1 > o.shift(2)) & (o1 < c.shift(2)))
    elif name == "three_black_crows":
        out = (red & red1 & red.shift(2, fill_value=False)
               & (c < c1) & (c1 < c.shift(2))
               & (o < o1) & (o > c1) & (o1 < o.shift(2)) & (o1 > c.shift(2)))
    elif name == "piercing":
        out = red1 & green & (o < c1) & (c > (o1 + c1) / 2) & (c < o1)
    elif name == "dark_cloud_cover":
        out = green1 & red & (o > c1) & (c < (o1 + c1) / 2) & (c > o1)
    else:
        raise ValueError(f"unknown pattern {name}")
    return out.fillna(False)
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_patterns.py -v` → 5 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/patterns.py tests/test_scanner_patterns.py
git commit -m "feat(scanner): vectorized candlestick patterns"
```

---

### Task 6: Scan engine

**Files:**
- Create: `apps/api/scanner/engine.py`
- Test: `tests/test_scanner_engine.py`

**Interfaces:**
- Consumes: `ScannerStore` (Task 1), `Group/Condition/Operand` (Task 2), `Panel`, `eval_operand`, `describe` (Task 4)
- Produces:
  - `ScanEngine(store: ScannerStore)` with `run(definition: Group) -> dict`:
    `{"data_as_of": str, "universe": int, "matches": [{"symbol", "name", "sector", "close", "change_pct", "volume", "rvol", "values": {label: float}}]}` — matches sorted by `change_pct` desc, values rounded to 4 decimals
  - `get_engine() -> ScanEngine` (module singleton bound to `get_scanner_store()`)

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_engine.py`:

```python
from __future__ import annotations

from apps.api.scanner.engine import ScanEngine
from apps.api.scanner.schema import parse_definition
from tests.scanner_utils import bars_long, make_store


def run(store, definition: dict) -> dict:
    return ScanEngine(store).run(parse_definition(definition))


def C(**kw):
    base = {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}
    base.update(kw)
    return base


def AND(*children):
    return {"logic": "AND", "children": list(children)}


def test_simple_threshold():
    store = make_store(tmp_path := __import__("pathlib").Path(".pytest_store1"),
                       {"HI": [101.0] * 30, "LO": [99.0] * 30})
    res = run(store, AND(C()))
    assert [m["symbol"] for m in res["matches"]] == ["HI"]
    assert res["matches"][0]["values"]["close [1d]"] == 101.0


def test_cross_fires_only_on_crossing_bar(tmp_path):
    crossing = [95.0] * 28 + [99.0, 101.0]   # crosses 100 at last bar
    above = [101.0] * 30                      # already above — no cross
    store = make_store(tmp_path, {"X": crossing, "A": above})
    res = run(store, AND(C(op="crosses_above")))
    assert [m["symbol"] for m in res["matches"]] == ["X"]


def test_for_n_bars_streak(tmp_path):
    store = make_store(tmp_path, {"S": [99.0] * 27 + [101, 102, 103],
                                  "N": [99.0] * 28 + [101, 102]})
    res = run(store, AND(dict(C(), for_n_bars=3)))
    assert [m["symbol"] for m in res["matches"]] == ["S"]


def test_or_group_and_nesting(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [50.0] * 30, "C": [99.0] * 30})
    d = {"logic": "OR", "children": [
        C(), C(op="<", right={"const": 60}),
    ]}
    res = run(store, d)
    assert {m["symbol"] for m in res["matches"]} == {"A", "B"}


def test_missing_data_excluded(tmp_path):
    store = make_store(tmp_path, {"FULL": [101.0] * 300, "SHORT": [101.0] * 5})
    d = AND(C(left={"fn": "SMA", "of": "close", "period": 200}, op=">",
              right={"const": 0}))
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["FULL"]


def test_multi_timeframe_and(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [101.0] * 30})
    store.upsert_bars("15m", bars_long("A", [201.0] * 30, freq_minutes=15))
    store.upsert_bars("15m", bars_long("B", [10.0] * 30, freq_minutes=15))
    d = AND(C(), C(timeframe="15m", right={"const": 200}))
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["A"]


def test_meta_condition(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30})
    d = AND(C(left={"meta": "sector"}, op="==", right={"const_str": "Test"}))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]
    d2 = AND(C(left={"meta": "sector"}, op="==", right={"const_str": "Banking"}))
    assert run(store, d2)["matches"] == []


def test_fundamental_condition(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30})
    d = AND(C(left={"fundamental": "market_cap"}, op=">", right={"const": 1000}))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]


def test_weekly_resample(tmp_path):
    store = make_store(tmp_path, {"A": [float(100 + i) for i in range(60)]})
    d = AND(C(timeframe="1w"))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]


def test_result_shape(tmp_path):
    store = make_store(tmp_path, {"A": [100.0] * 29 + [110.0]})
    res = run(store, AND(C()))
    m = res["matches"][0]
    assert m["name"] == "A" and m["sector"] == "Test"
    assert m["change_pct"] == 10.0
    assert m["rvol"] == 1.0
    assert res["data_as_of"]
```

Note the first test writes to `.pytest_store1` in cwd — change it to use the `tmp_path` fixture like every other test when implementing:

```python
def test_simple_threshold(tmp_path):
    store = make_store(tmp_path, {"HI": [101.0] * 30, "LO": [99.0] * 30})
    ...
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/engine.py`:

```python
"""Evaluate a scanner definition over the whole universe.

Panels load per timeframe (last ~320 bars x all symbols); every operand
computes once as a wide frame; the boolean tree reduces per-symbol at the
latest bar. Missing data never matches.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from apps.api.scanner.indicators import Panel, describe, eval_operand
from apps.api.scanner.schema import Condition, Group, Operand
from apps.api.scanner.store import ScannerStore, get_scanner_store

_RESAMPLE = {"1w": "W", "1mo": "ME"}
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


class ScanEngine:
    def __init__(self, store: ScannerStore) -> None:
        self._store = store
        self._panels: Dict[str, Panel] = {}
        self._version = -1
        self._lock = threading.Lock()

    # -- panels -----------------------------------------------------------
    def _panel(self, timeframe: str) -> Panel:
        with self._lock:
            v = self._store.version()
            if v != self._version:
                self._panels.clear()
                self._version = v
            if timeframe in self._panels:
                return self._panels[timeframe]
            base_tf = "1d" if timeframe in _RESAMPLE else timeframe
            long = self._store.load_bars(base_tf)
            inst = self._store.instruments_df()
            frames = {}
            for fld in ("open", "high", "low", "close", "volume"):
                wide = long.pivot(index="ts", columns="symbol", values=fld)
                wide.index = pd.to_datetime(wide.index)
                frames[fld] = wide.sort_index()
            if timeframe in _RESAMPLE:
                frames = {f: frames[f].resample(_RESAMPLE[timeframe]).agg(_AGG[f])
                          for f in frames}
            fund = pd.DataFrame(list(inst["fundamentals"]), index=inst.index) \
                if len(inst) else pd.DataFrame()
            if len(inst) and "market_cap" not in fund:
                fund["market_cap"] = inst["market_cap"]
            elif len(inst):
                fund["market_cap"] = fund["market_cap"].fillna(inst["market_cap"])
            meta = inst[["sector", "industry", "index_memberships", "fno"]] \
                if len(inst) else pd.DataFrame()
            panel = Panel(fundamentals=fund, meta=meta, **frames)
            self._panels[timeframe] = panel
            return panel

    # -- evaluation ---------------------------------------------------------
    def run(self, definition: Group) -> dict:
        mask, values = self._group(definition)
        d1 = self._panel("1d")
        symbols = [s for s in mask.index[mask] if s in d1.close.columns]

        close = d1.close.iloc[-1]
        prev = d1.close.iloc[-2] if len(d1.close) > 1 else close
        vol = d1.volume.iloc[-1]
        vol_avg = d1.volume.rolling(20).mean().iloc[-1]
        inst = self._store.instruments_df()

        matches = []
        for s in symbols:
            row = inst.loc[s] if s in inst.index else None
            matches.append({
                "symbol": s,
                "name": (row["name"] if row is not None else s) or s,
                "sector": row["sector"] if row is not None else None,
                "close": _r(close.get(s)),
                "change_pct": _r((close.get(s) / prev.get(s) - 1) * 100
                                 if prev.get(s) else None),
                "volume": _r(vol.get(s)),
                "rvol": _r(vol.get(s) / vol_avg.get(s)
                           if vol_avg.get(s) and vol_avg.get(s) > 0 else None),
                "values": {k: _r(v.get(s)) for k, v in values.items()},
            })
        matches.sort(key=lambda m: m["change_pct"] if m["change_pct"] is not None else -1e9,
                     reverse=True)
        data_as_of = max((str(self._store.latest_ts(tf)) for tf in ("1d", "1h", "15m", "5m")
                          if self._store.latest_ts(tf)), default="")
        return {"data_as_of": data_as_of, "universe": int(d1.close.shape[1]),
                "matches": matches}

    def _group(self, g: Group) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        masks, values = [], {}
        for child in g.children:
            m, v = (self._group(child) if isinstance(child, Group)
                    else self._condition(child))
            masks.append(m)
            values.update(v)
        idx = masks[0].index
        for m in masks[1:]:
            idx = idx.union(m.index)
        aligned = [m.reindex(idx, fill_value=False) for m in masks]
        out = aligned[0]
        for m in aligned[1:]:
            out = (out & m) if g.logic == "AND" else (out | m)
        return out, values

    def _condition(self, c: Condition) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        panel = self._panel(c.timeframe)

        # Meta conditions are time-invariant string comparisons.
        if c.left.meta is not None:
            return self._meta_condition(c, panel), {}

        frame = self._cond_frame(c, panel)
        if c.for_n_bars:
            mask = frame.tail(c.for_n_bars).all()
        else:
            mask = frame.iloc[-1] if len(frame) else pd.Series(dtype=bool)

        values: Dict[str, pd.Series] = {}
        for side in (c.left, c.right):
            if side is None or side.const is not None:
                continue
            v = eval_operand(side, panel, self._cond_frame_for(panel))
            if isinstance(v, pd.DataFrame) and len(v):
                values[f"{describe(side)} [{c.timeframe}]"] = v.iloc[-1]
        return mask.fillna(False).astype(bool), values

    def _meta_condition(self, c: Condition, panel: Panel) -> pd.Series:
        left = eval_operand(c.left, panel)
        right = eval_operand(c.right, panel)
        if c.left.meta == "index":
            vals = right if isinstance(right, list) else [right]
            mask = left.map(lambda mem: any(v in (mem or []) for v in vals))
        elif c.op == "in":
            mask = left.isin(right if isinstance(right, list) else [right])
        elif c.op == "!=":
            mask = left != right
        else:  # ==
            mask = left == right
        return mask.fillna(False).astype(bool)

    def _cond_frame_for(self, panel: Panel):
        def _eval(cond: Condition) -> pd.DataFrame:
            return self._cond_frame(cond, panel)
        return _eval

    def _cond_frame(self, c: Condition, panel: Panel) -> pd.DataFrame:
        """Raw per-bar boolean frame for a condition (validity included)."""
        cond_eval = self._cond_frame_for(panel)
        if c.left.pattern is not None:
            return eval_operand(c.left, panel, cond_eval)
        L = eval_operand(c.left, panel, cond_eval)
        R = eval_operand(c.right, panel, cond_eval)
        ops = {">": lambda a, b: a > b, "<": lambda a, b: a < b,
               ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
               "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
        if c.op == "crosses_above":
            res = _sh(L, R, lambda a, b: a > b, lambda a, b: a <= b)
        elif c.op == "crosses_below":
            res = _sh(L, R, lambda a, b: a < b, lambda a, b: a >= b)
        else:
            res = ops[c.op](L, R)
        valid = _notna(L) & _notna(R)
        if isinstance(res, pd.DataFrame):
            return (res & valid).fillna(False)
        # Scalar-vs-scalar degenerate condition: broadcast over the panel.
        return panel.close.notna() & bool(res)


def _sh(L, R, now_cmp, prev_cmp):
    Ls = L.shift(1) if isinstance(L, pd.DataFrame) else L
    Rs = R.shift(1) if isinstance(R, pd.DataFrame) else R
    return now_cmp(L, R) & prev_cmp(Ls, Rs)


def _notna(x):
    return x.notna() if isinstance(x, pd.DataFrame) else True


def _r(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v):
        return None
    return round(float(v), 4)


_engine: Optional[ScanEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> ScanEngine:
    global _engine
    with _engine_lock:
        store = get_scanner_store()
        if _engine is None or _engine._store is not store:
            _engine = ScanEngine(store)
        return _engine
```

- [ ] **Step 4: Run to verify PASS**

Run: `pytest tests/test_scanner_engine.py tests/test_scanner_indicators.py -v` — Expected: all PASS. Debug engine/indicator interactions here, not later in the API task.

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/engine.py tests/test_scanner_engine.py
git commit -m "feat(scanner): universe scan engine with caching and multi-timeframe eval"
```

---

### Task 7: Universe seed + enrichment

**Files:**
- Create: `apps/api/scanner/universe.py`, `apps/api/scanner/data/nse_universe.csv`
- Test: `tests/test_scanner_universe.py`

**Interfaces:**
- Consumes: `ScannerStore` (Task 1)
- Produces: `seed_universe(store) -> int` (rows upserted from the bundled CSV), `enrich_universe(store, limit: int | None = None) -> int` (yfinance info sweep), `refresh_universe_csv() -> None` (downloads a fresh NIFTY500 list — dev utility, not called at runtime)

- [ ] **Step 1: Generate the bundled universe CSV**

Run this once (network access to NSE archives needed; the CSV is committed so runtime never depends on NSE being reachable):

```bash
python - <<'EOF'
import csv, io, urllib.request
URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
rows = list(csv.DictReader(io.StringIO(raw)))
with open("apps/api/scanner/data/nse_universe.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "name", "industry", "index_memberships"])
    for r in rows:
        w.writerow([r["Symbol"].strip(), r["Company Name"].strip(),
                    r.get("Industry", "").strip(), "NIFTY500"])
print(f"wrote {len(rows)} rows")
EOF
```

If NSE blocks the request (datacenter IP), fetch the same file from `https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv` or run the command from a residential connection — the output CSV is what matters, not the fetch path. `mkdir -p apps/api/scanner/data` first. Expect ~500 rows.

- [ ] **Step 2: Write the failing tests**

`tests/test_scanner_universe.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from apps.api.scanner.universe import enrich_universe, seed_universe
from tests.scanner_utils import make_store


def test_seed_from_bundled_csv(tmp_path):
    store = make_store(tmp_path)
    n = seed_universe(store)
    assert n > 400  # NIFTY500 snapshot
    inst = store.instruments_df()
    assert (inst["yf_symbol"].str.endswith(".NS")).all()
    assert "NIFTY500" in inst["index_memberships"].iloc[0]


def test_seed_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    seed_universe(store)
    n1 = len(store.instruments_df())
    seed_universe(store)
    assert len(store.instruments_df()) == n1


def test_enrich_writes_fundamentals(tmp_path):
    store = make_store(tmp_path, {"TCS": [100.0]})
    fake = MagicMock()
    fake.info = {"sector": "Technology", "industry": "IT Services",
                 "marketCap": 12_000_000_000_000, "trailingPE": 28.5,
                 "priceToBook": 12.0, "returnOnEquity": 0.45,
                 "dividendYield": 0.012, "trailingEps": 130.0,
                 "debtToEquity": 8.0, "revenueGrowth": 0.06}
    with patch("apps.api.scanner.universe.yf.Ticker", return_value=fake):
        n = enrich_universe(store)
    assert n == 1
    inst = store.instruments_df()
    assert inst.loc["TCS", "sector"] == "Technology"
    assert inst.loc["TCS", "fundamentals"]["pe"] == 28.5


def test_enrich_survives_per_symbol_failure(tmp_path):
    store = make_store(tmp_path, {"AAA": [1.0], "BBB": [1.0]})
    def boom_then_ok(sym):
        if sym == "AAA.NS":
            raise RuntimeError("rate limited")
        m = MagicMock(); m.info = {"sector": "X", "marketCap": 1}
        return m
    with patch("apps.api.scanner.universe.yf.Ticker", side_effect=boom_then_ok):
        n = enrich_universe(store)
    assert n == 1
```

- [ ] **Step 3: Run to verify FAIL** — ImportError

- [ ] **Step 4: Implement**

`apps/api/scanner/universe.py`:

```python
"""NSE universe: bundled NIFTY500 snapshot + weekly yfinance enrichment.

The CSV ships in the repo so a fresh deploy scans immediately without
depending on NSE archives being reachable from a datacenter IP.
refresh_universe_csv() regenerates it — run by hand, review the diff, commit.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from pathlib import Path

import yfinance as yf

from apps.api.scanner.store import ScannerStore

logger = logging.getLogger(__name__)

DATA_CSV = Path(__file__).parent / "data" / "nse_universe.csv"
NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

#: yfinance info key -> our fundamentals key
_FUND_KEYS = {"trailingPE": "pe", "priceToBook": "pb", "returnOnEquity": "roe",
              "dividendYield": "dividend_yield", "trailingEps": "eps",
              "debtToEquity": "debt_to_equity", "revenueGrowth": "revenue_growth"}


def seed_universe(store: ScannerStore) -> int:
    rows = []
    with DATA_CSV.open() as f:
        for r in csv.DictReader(f):
            rows.append({
                "symbol": r["symbol"],
                "yf_symbol": f"{r['symbol']}.NS",
                "name": r["name"],
                "industry": r["industry"] or None,
                "index_memberships": r["index_memberships"].split("|"),
            })
    store.upsert_instruments(rows)
    return len(rows)


def enrich_universe(store: ScannerStore, limit: int | None = None,
                    sleep_s: float = 0.25) -> int:
    """Fill sector/mcap/fundamentals from yfinance. Weekly cadence; failures skip."""
    inst = store.instruments_df()
    done = 0
    for symbol, row in inst.iterrows():
        if limit is not None and done >= limit:
            break
        try:
            info = yf.Ticker(row["yf_symbol"]).info or {}
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the sweep
            logger.warning("enrich %s failed: %s", symbol, exc)
            continue
        fundamentals = {ours: info[theirs] for theirs, ours in _FUND_KEYS.items()
                        if info.get(theirs) is not None}
        store.upsert_instruments([{
            "symbol": symbol,
            "yf_symbol": row["yf_symbol"],
            "name": row["name"],
            "sector": info.get("sector"),
            "industry": info.get("industry") or row.get("industry"),
            "market_cap": info.get("marketCap"),
            "index_memberships": row["index_memberships"],
            "fno": bool(row.get("fno", False)),
            "fundamentals": fundamentals,
        }])
        done += 1
        if sleep_s and limit is None:
            time.sleep(sleep_s)  # be polite to Yahoo on the full sweep
    return done


def refresh_universe_csv() -> None:
    """Dev utility: regenerate the bundled CSV from NSE archives."""
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    with DATA_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "industry", "index_memberships"])
        for r in rows:
            w.writerow([r["Symbol"].strip(), r["Company Name"].strip(),
                        r.get("Industry", "").strip(), "NIFTY500"])
```

Note: `enrich_universe` passes `sleep_s` only on full sweeps; tests use `limit`-free calls with mocked `yf.Ticker`, so set `sleep_s=0.25` default but skip sleeping when the mock returns instantly is NOT needed — tests patch `yf.Ticker` and 2 symbols × 0.25s is fine. If test runtime bothers you, pass `sleep_s=0` in the tests.

- [ ] **Step 5: Run to verify PASS** — `pytest tests/test_scanner_universe.py -v` → 4 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api/scanner/universe.py apps/api/scanner/data/nse_universe.csv tests/test_scanner_universe.py
git commit -m "feat(scanner): NSE universe seed + yfinance enrichment"
```

---

### Task 8: Ingest — yfinance batch bars + async loop

**Files:**
- Create: `apps/api/scanner/ingest.py`
- Test: `tests/test_scanner_ingest.py`

**Interfaces:**
- Consumes: `ScannerStore`, `seed_universe`, `enrich_universe`, `calendar.is_market_open`
- Produces:
  - `refresh_timeframe(store, timeframe: str) -> int` (bars written; `TF_FETCH` maps tf → yf period/interval)
  - `refresh_all(store) -> None` (all four timeframes — initial backfill)
  - `ingest_loop() -> None` (async; started from app lifespan; env `SCANNER_INGEST=0` disables — checked by the caller, not the loop)
  - `INTRADAY_REFRESH_SECONDS = 600`

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_ingest.py`:

```python
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from apps.api.scanner.ingest import refresh_timeframe
from tests.scanner_utils import make_store


def yf_frame(symbols: list[str], closes: list[float]) -> pd.DataFrame:
    """Mimic yf.download(group_by='ticker') multi-index output."""
    idx = pd.date_range("2026-08-03", periods=len(closes), freq="D")
    frames = {}
    for s in symbols:
        frames[s] = pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes],
                                  "Low": [c * 0.99 for c in closes], "Close": closes,
                                  "Volume": [1000] * len(closes)}, index=idx)
    return pd.concat(frames, axis=1)


def test_refresh_writes_bars(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0], "INFY": [1.0]})
    data = yf_frame(["TCS.NS", "INFY.NS"], [100.0, 101.0, 102.0])
    with patch("apps.api.scanner.ingest.yf.download", return_value=data) as dl:
        n = refresh_timeframe(store, "1d")
    assert n == 6  # 2 symbols x 3 bars
    df = store.load_bars("1d")
    assert set(df["symbol"]) == {"TCS", "INFY"}
    assert dl.call_args.kwargs["interval"] == "1d"


def test_refresh_skips_symbols_with_no_data(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0], "DEAD": [1.0]})
    data = yf_frame(["TCS.NS", "DEAD.NS"], [100.0, 101.0])
    data[("DEAD.NS", "Close")] = float("nan")
    with patch("apps.api.scanner.ingest.yf.download", return_value=data):
        refresh_timeframe(store, "1d")
    assert set(store.load_bars("1d")["symbol"]) == {"TCS"}


def test_refresh_prunes_to_retention(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0]})
    data = yf_frame(["TCS.NS"], [float(i) for i in range(400)])
    with patch("apps.api.scanner.ingest.yf.download", return_value=data):
        refresh_timeframe(store, "1d")
    assert len(store.load_bars("1d", limit=1000)) == 320
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/ingest.py`:

```python
"""Scheduled yfinance ingest: EOD after close, delayed intraday during hours.

Runs as an asyncio task inside the API process (same pattern as the jobs
runner). yfinance intraday is 15-20 min delayed — that's the accepted product
trade-off; see the spec. All fetch work happens in a thread via
asyncio.to_thread so the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pandas as pd
import yfinance as yf

from apps.api.scanner.calendar import IST, is_market_open, is_trading_day
from apps.api.scanner.store import ScannerStore, get_scanner_store
from apps.api.scanner.universe import enrich_universe, seed_universe

logger = logging.getLogger(__name__)

#: timeframe -> (yfinance period, yfinance interval)
TF_FETCH = {"1d": ("2y", "1d"), "1h": ("730d", "1h"),
            "15m": ("60d", "15m"), "5m": ("60d", "5m")}
CHUNK = 100
RETENTION = 320
INTRADAY_REFRESH_SECONDS = 600
EOD_HOUR_IST = 18  # refresh daily bars after 18:00 IST


def refresh_timeframe(store: ScannerStore, timeframe: str) -> int:
    period, interval = TF_FETCH[timeframe]
    inst = store.instruments_df()
    if inst.empty:
        return 0
    yf_to_ours = dict(zip(inst["yf_symbol"], inst.index))
    written = 0
    yf_symbols = list(yf_to_ours)
    for i in range(0, len(yf_symbols), CHUNK):
        chunk = yf_symbols[i:i + CHUNK]
        try:
            data = yf.download(tickers=" ".join(chunk), period=period,
                               interval=interval, group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as exc:  # noqa: BLE001 — partial universe beats no universe
            logger.warning("yf.download %s chunk %d failed: %s", timeframe, i, exc)
            continue
        long = _to_long(data, chunk, yf_to_ours)
        if not long.empty:
            store.upsert_bars(timeframe, long)
            written += len(long)
    store.prune_bars(timeframe, keep=RETENTION)
    return written


def _to_long(data: pd.DataFrame, chunk: list[str], yf_to_ours: dict) -> pd.DataFrame:
    frames = []
    for yf_sym in chunk:
        try:
            df = data[yf_sym] if isinstance(data.columns, pd.MultiIndex) else data
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        frames.append(pd.DataFrame({
            "symbol": yf_to_ours[yf_sym],
            "ts": [t.isoformat() for t in df.index],
            "open": df["Open"].to_numpy(), "high": df["High"].to_numpy(),
            "low": df["Low"].to_numpy(), "close": df["Close"].to_numpy(),
            "volume": df["Volume"].to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def refresh_all(store: ScannerStore) -> None:
    for tf in TF_FETCH:
        n = refresh_timeframe(store, tf)
        logger.info("scanner ingest: %s -> %d bars", tf, n)


async def ingest_loop() -> None:
    store = get_scanner_store()
    if store.instruments_df().empty:
        await asyncio.to_thread(seed_universe, store)
        logger.info("scanner universe seeded")
    if store.latest_ts("1d") is None:
        logger.info("scanner initial backfill starting")
        await asyncio.to_thread(refresh_all, store)

    last_intraday = 0.0
    eod_done_for = ""
    enrich_done_for = ""
    while True:
        try:
            now = datetime.now(IST)
            loop_t = asyncio.get_running_loop().time()
            if is_market_open(now) and loop_t - last_intraday > INTRADAY_REFRESH_SECONDS:
                last_intraday = loop_t
                for tf in ("5m", "15m", "1h"):
                    await asyncio.to_thread(refresh_timeframe, store, tf)
            today = now.date().isoformat()
            if (is_trading_day(now.date()) and now.hour >= EOD_HOUR_IST
                    and eod_done_for != today):
                eod_done_for = today
                await asyncio.to_thread(refresh_timeframe, store, "1d")
            # Weekly fundamentals sweep on Saturdays.
            week = f"{now.isocalendar().year}-{now.isocalendar().week}"
            if now.weekday() == 5 and enrich_done_for != week:
                enrich_done_for = week
                await asyncio.to_thread(enrich_universe, store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive any single failure
            logger.exception("scanner ingest cycle failed")
        await asyncio.sleep(60)
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_ingest.py -v` → 3 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/ingest.py tests/test_scanner_ingest.py
git commit -m "feat(scanner): yfinance batch ingest with EOD/intraday loop"
```

---

### Task 9: Prebuilt scanners

**Files:**
- Create: `apps/api/scanner/prebuilt/` — 10 JSON files (below), `apps/api/scanner/prebuilt/__init__.py` is NOT needed (data dir), seeding fn goes in `apps/api/scanner/seed.py`
- Test: `tests/test_scanner_prebuilt.py`

**Interfaces:**
- Produces: `apps.api.scanner.seed.seed_prebuilt(store) -> int` (validates each JSON via `parse_definition`, upserts by name)

- [ ] **Step 1: Write the failing test**

`tests/test_scanner_prebuilt.py`:

```python
from __future__ import annotations

from apps.api.scanner.seed import seed_prebuilt
from tests.scanner_utils import make_store


def test_all_prebuilt_seed_and_validate(tmp_path):
    store = make_store(tmp_path)
    n = seed_prebuilt(store)
    assert n == 10
    names = {s["name"] for s in store.list_scanners("nobody")}
    assert "Golden cross" in names and "Volume spike" in names
    # Idempotent: re-seed updates, never duplicates.
    seed_prebuilt(store)
    assert len(store.list_scanners("nobody")) == 10
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Create the JSON files**

Each file: `{"name", "description", "definition"}`. Create all 10 in `apps/api/scanner/prebuilt/`:

`golden_cross.json`
```json
{"name": "Golden cross", "description": "SMA(50) crosses above SMA(200) on the daily.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"fn": "SMA", "of": "close", "period": 50},
    "op": "crosses_above", "right": {"fn": "SMA", "of": "close", "period": 200}}]}}
```

`week52_high_breakout.json`
```json
{"name": "52-week-high breakout", "description": "Close breaks the prior 250-bar high with above-average volume.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
    "right": {"fn": "HIGHEST", "of": "high", "period": 250, "bars_ago": 1}},
   {"timeframe": "1d", "left": {"field": "volume"}, "op": ">",
    "right": {"fn": "SMA", "of": "volume", "period": 20}}]}}
```

`rsi_oversold_bounce.json`
```json
{"name": "RSI oversold bounce", "description": "RSI(14) crosses back above 30 from oversold.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"fn": "RSI", "of": "close", "period": 14},
    "op": "crosses_above", "right": {"const": 30}}]}}
```

`volume_spike.json`
```json
{"name": "Volume spike", "description": "Volume above 2x its 20-day average with a positive close.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "volume"}, "op": ">",
    "right": {"expr": "*", "args": [{"const": 2}, {"fn": "SMA", "of": "volume", "period": 20}]}},
   {"timeframe": "1d", "left": {"field": "change_pct"}, "op": ">", "right": {"const": 0}}]}}
```

`macd_bull_cross.json`
```json
{"name": "MACD bullish cross", "description": "MACD line crosses above its signal line on the daily.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"fn": "MACD"}, "op": "crosses_above",
    "right": {"fn": "MACD", "component": "signal"}}]}}
```

`bb_squeeze_breakout.json`
```json
{"name": "Bollinger squeeze breakout", "description": "Close breaks the upper band after a tight-band squeeze.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
    "right": {"fn": "BBANDS", "of": "close", "period": 20, "component": "upper"}},
   {"timeframe": "1d", "left": {"fn": "BBWIDTH", "of": "close", "period": 20, "bars_ago": 1},
    "op": "<", "right": {"const": 8}}]}}
```

`supertrend_flip.json`
```json
{"name": "Supertrend flip", "description": "Close crosses above the Supertrend(10, 3) line.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "close"}, "op": "crosses_above",
    "right": {"fn": "SUPERTREND", "period": 10}}]}}
```

`gap_up_volume.json`
```json
{"name": "Gap up with volume", "description": "Opened 2%+ above yesterday's close and holding, on strong volume.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "gap_pct"}, "op": ">", "right": {"const": 2}},
   {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"field": "open"}},
   {"timeframe": "1d", "left": {"field": "volume"}, "op": ">",
    "right": {"expr": "*", "args": [{"const": 1.5}, {"fn": "SMA", "of": "volume", "period": 20}]}}]}}
```

`momentum_above_200sma.json`
```json
{"name": "Momentum above 200 SMA", "description": "Uptrend stack: close > SMA(200), EMA(20) > EMA(50), RSI > 60.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
    "right": {"fn": "SMA", "of": "close", "period": 200}},
   {"timeframe": "1d", "left": {"fn": "EMA", "of": "close", "period": 20}, "op": ">",
    "right": {"fn": "EMA", "of": "close", "period": 50}},
   {"timeframe": "1d", "left": {"fn": "RSI", "of": "close", "period": 14}, "op": ">",
    "right": {"const": 60}}]}}
```

`three_white_soldiers.json`
```json
{"name": "Three white soldiers", "description": "Three consecutive strong bullish candles.",
 "definition": {"logic": "AND", "children": [
   {"timeframe": "1d", "left": {"pattern": "three_white_soldiers"}}]}}
```

- [ ] **Step 4: Implement the seeder**

`apps/api/scanner/seed.py`:

```python
"""Load prebuilt scanner JSONs, validate, upsert. Runs at app startup."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from apps.api.scanner.schema import parse_definition
from apps.api.scanner.store import ScannerStore

logger = logging.getLogger(__name__)
PREBUILT_DIR = Path(__file__).parent / "prebuilt"


def seed_prebuilt(store: ScannerStore) -> int:
    count = 0
    for path in sorted(PREBUILT_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        parse_definition(data["definition"])  # a broken seed should fail startup loudly
        store.upsert_prebuilt(data["name"], data["description"], data["definition"])
        count += 1
    logger.info("seeded %d prebuilt scanners", count)
    return count
```

- [ ] **Step 5: Run to verify PASS** — `pytest tests/test_scanner_prebuilt.py -v` → 1 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/api/scanner/prebuilt/ apps/api/scanner/seed.py tests/test_scanner_prebuilt.py
git commit -m "feat(scanner): 10 prebuilt scanners + startup seeding"
```

---

### Task 10: REST API

**Files:**
- Create: `apps/api/routes/scanners.py`
- Test: `tests/test_scanner_api.py`

**Interfaces:**
- Consumes: `get_scanner_store`, `get_engine`, `parse_definition`/`DefinitionError`, `current_user_id` (`apps.api.auth`)
- Produces (all under `/api` prefix added at include time):
  - `GET /scanners` → `[{id, name, description, prebuilt, definition, created_at, updated_at}]`
  - `POST /scanners` `{name, description?, definition}` → 201 + scanner
  - `PUT /scanners/{sid}` same body → scanner; `DELETE /scanners/{sid}` → 204
  - `POST /scanners/{sid}/run` → scan result (engine `run()` shape)
  - `POST /scanners/preview` `{definition}` → scan result
  - `POST /scanners/nl` `{prompt}` → `{definition, explanation}` (Task 11 wires the real generator; this task stubs the import)
  - Ownership: 404 on another user's scanner (don't leak existence), 403 on mutating a prebuilt, 422 on invalid definitions

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_api.py`:

```python
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.routes import scanners as scanners_module
from tests.scanner_utils import make_store

DEF = {"logic": "AND", "children": [
    {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}]}


@pytest.fixture()
def env(tmp_path):
    store = make_store(tmp_path, {"HI": [101.0] * 30, "LO": [99.0] * 30})
    current_user = {"id": "user_a"}
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = current_user["id"]
        return await call_next(request)

    app.include_router(scanners_module.router, prefix="/api")
    return TestClient(app), store, current_user


def test_crud_and_ownership(env):
    client, store, user = env
    r = client.post("/api/scanners", json={"name": "Mine", "definition": DEF})
    assert r.status_code == 201
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/scanners").json())

    user["id"] = "user_b"
    assert not any(s["id"] == sid for s in client.get("/api/scanners").json())
    assert client.put(f"/api/scanners/{sid}",
                      json={"name": "Stolen", "definition": DEF}).status_code == 404
    assert client.delete(f"/api/scanners/{sid}").status_code == 404
    assert client.post(f"/api/scanners/{sid}/run").status_code == 404

    user["id"] = "user_a"
    assert client.put(f"/api/scanners/{sid}",
                      json={"name": "Renamed", "definition": DEF}).status_code == 200
    assert client.delete(f"/api/scanners/{sid}").status_code == 204


def test_prebuilt_visible_but_read_only(env):
    client, store, _ = env
    store.upsert_prebuilt("Golden cross", "d", DEF)
    listed = client.get("/api/scanners").json()
    pb = next(s for s in listed if s["prebuilt"])
    assert client.put(f"/api/scanners/{pb['id']}",
                      json={"name": "Hacked", "definition": DEF}).status_code == 403
    assert client.delete(f"/api/scanners/{pb['id']}").status_code == 403
    assert client.post(f"/api/scanners/{pb['id']}/run").status_code == 200


def test_run_and_preview_return_matches(env):
    client, _, _ = env
    r = client.post("/api/scanners/preview", json={"definition": DEF})
    assert r.status_code == 200
    assert [m["symbol"] for m in r.json()["matches"]] == ["HI"]

    created = client.post("/api/scanners", json={"name": "S", "definition": DEF}).json()
    r2 = client.post(f"/api/scanners/{created['id']}/run")
    assert [m["symbol"] for m in r2.json()["matches"]] == ["HI"]


def test_invalid_definition_422(env):
    client, _, _ = env
    bad = {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "nope"}, "op": ">", "right": {"const": 1}}]}
    assert client.post("/api/scanners/preview", json={"definition": bad}).status_code == 422
    assert client.post("/api/scanners", json={"name": "x", "definition": bad}).status_code == 422
    huge = {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
         "right": {"const": 1}}] * 60}
    assert client.post("/api/scanners/preview", json={"definition": huge}).status_code == 422
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Implement**

`apps/api/routes/scanners.py`:

```python
"""REST endpoints for the stock scanner.

Routes:
  GET    /api/scanners               list (prebuilt + own)
  POST   /api/scanners               create
  PUT    /api/scanners/{sid}         update own
  DELETE /api/scanners/{sid}         delete own
  POST   /api/scanners/{sid}/run     run saved scanner
  POST   /api/scanners/preview       run an unsaved definition
  POST   /api/scanners/nl            natural language -> definition
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from apps.api.auth import current_user_id
from apps.api.scanner.engine import get_engine
from apps.api.scanner.schema import DefinitionError, Group, parse_definition
from apps.api.scanner.store import get_scanner_store

router = APIRouter()


class ScannerBody(BaseModel):
    name: str
    description: str = ""
    definition: Dict[str, Any]


class PreviewBody(BaseModel):
    definition: Dict[str, Any]


class NlBody(BaseModel):
    prompt: str


def _parse_or_422(definition: Dict[str, Any]) -> Group:
    try:
        return parse_definition(definition)
    except DefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[:5])


def _owned_or_error(sid: str, user_id: str, *, for_write: bool) -> Dict[str, Any]:
    scanner = get_scanner_store().get_scanner(sid)
    if scanner is None:
        raise HTTPException(status_code=404, detail="scanner not found")
    if scanner["prebuilt"]:
        if for_write:
            raise HTTPException(status_code=403, detail="prebuilt scanners are read-only")
        return scanner
    if scanner["user_id"] != user_id:
        # 404, not 403: don't leak that another user's scanner id exists.
        raise HTTPException(status_code=404, detail="scanner not found")
    return scanner


@router.get("/scanners")
def list_scanners(user_id: str = Depends(current_user_id)) -> list:
    return get_scanner_store().list_scanners(user_id)


@router.post("/scanners", status_code=201)
def create_scanner(body: ScannerBody, user_id: str = Depends(current_user_id)) -> dict:
    _parse_or_422(body.definition)
    store = get_scanner_store()
    sid = store.create_scanner(user_id, body.name, body.description, body.definition)
    return store.get_scanner(sid)


@router.put("/scanners/{sid}")
def update_scanner(sid: str, body: ScannerBody,
                   user_id: str = Depends(current_user_id)) -> dict:
    _owned_or_error(sid, user_id, for_write=True)
    _parse_or_422(body.definition)
    store = get_scanner_store()
    store.update_scanner(sid, body.name, body.description, body.definition)
    return store.get_scanner(sid)


@router.delete("/scanners/{sid}", status_code=204)
def delete_scanner(sid: str, user_id: str = Depends(current_user_id)) -> None:
    _owned_or_error(sid, user_id, for_write=True)
    get_scanner_store().delete_scanner(sid)


@router.post("/scanners/preview")
def preview(body: PreviewBody, user_id: str = Depends(current_user_id)) -> dict:
    return get_engine().run(_parse_or_422(body.definition))


@router.post("/scanners/{sid}/run")
def run_scanner(sid: str, user_id: str = Depends(current_user_id)) -> dict:
    scanner = _owned_or_error(sid, user_id, for_write=False)
    return get_engine().run(_parse_or_422(scanner["definition"]))


@router.post("/scanners/nl")
def nl_scanner(body: NlBody, user_id: str = Depends(current_user_id)) -> dict:
    from apps.api.scanner import nl  # late import: keeps langchain out of test startup
    try:
        definition, explanation = nl.generate_definition(body.prompt)
    except nl.NlGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"definition": definition, "explanation": explanation}
```

Note: `nl.py` doesn't exist yet — that's Task 11. The late import inside the handler means every other route works and tests pass now; only hitting `/scanners/nl` would fail. Do NOT add a stub module.

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_api.py -v` → 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/routes/scanners.py tests/test_scanner_api.py
git commit -m "feat(scanner): REST API with per-user ownership"
```

---

### Task 11: NL → definition

**Files:**
- Create: `apps/api/scanner/nl.py`
- Test: `tests/test_scanner_nl.py`

**Interfaces:**
- Consumes: `parse_definition`, `DEFINITION_JSON_SCHEMA`, constants from schema
- Produces: `generate_definition(prompt: str) -> tuple[dict, str]` (validated definition + one-line explanation), `NlGenerationError(Exception)`. Model via env `SCANNER_NL_MODEL` (default `gpt-5.4-mini`), server `OPENAI_API_KEY`. `_invoke(messages) -> str` is the seam tests patch.

- [ ] **Step 1: Write the failing tests**

`tests/test_scanner_nl.py`:

```python
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apps.api.scanner import nl

GOOD = json.dumps({
    "explanation": "Close above 200 SMA with RSI over 60.",
    "definition": {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
         "right": {"fn": "SMA", "of": "close", "period": 200}},
        {"timeframe": "1d", "left": {"fn": "RSI", "of": "close", "period": 14},
         "op": ">", "right": {"const": 60}}]},
})

BAD = json.dumps({"explanation": "nope", "definition": {
    "logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "closse"}, "op": ">", "right": {"const": 1}}]}})


def test_valid_output_passes_through():
    with patch.object(nl, "_invoke", return_value=GOOD):
        definition, explanation = nl.generate_definition("momentum stocks")
    assert definition["children"][0]["right"]["period"] == 200
    assert "RSI" in explanation


def test_invalid_then_valid_retries_once():
    with patch.object(nl, "_invoke", side_effect=[BAD, GOOD]) as mock:
        definition, _ = nl.generate_definition("momentum stocks")
    assert mock.call_count == 2
    assert definition["logic"] == "AND"


def test_two_failures_raise():
    with patch.object(nl, "_invoke", side_effect=[BAD, BAD]):
        with pytest.raises(nl.NlGenerationError):
            nl.generate_definition("momentum stocks")


def test_json_extracted_from_fenced_output():
    fenced = f"Here you go:\n```json\n{GOOD}\n```"
    with patch.object(nl, "_invoke", return_value=fenced):
        definition, _ = nl.generate_definition("x")
    assert definition["logic"] == "AND"
```

- [ ] **Step 2: Run to verify FAIL** — ImportError

- [ ] **Step 3: Implement**

`apps/api/scanner/nl.py`:

```python
"""Natural language -> scanner definition via one LLM call (+1 retry).

Output is validated with the same parse_definition the API uses, so the model
cannot smuggle in anything the engine wouldn't accept. The result is shown in
the builder for user review — never auto-run.
"""
from __future__ import annotations

import json
import os
import re

from apps.api.scanner.schema import (
    EXPR_OPS, FIELDS, FUNCTIONS, FUNDAMENTALS, METAS, PATTERNS, TIMEFRAMES,
    parse_definition,
)


class NlGenerationError(Exception):
    pass


_SYSTEM = f"""You convert plain-English stock screener descriptions into a JSON scanner definition.

Reply with ONLY a JSON object: {{"explanation": "<one sentence>", "definition": <Group>}}

A Group is {{"logic": "AND"|"OR", "children": [Group or Condition, ...]}}.
A Condition is {{"timeframe": tf, "left": Operand, "op": op, "right": Operand}} with optional "for_n_bars": n for streaks.
Pattern conditions are {{"timeframe": tf, "left": {{"pattern": name}}}} with no op/right.

Timeframes: {", ".join(TIMEFRAMES)} (default "1d").
Operators: > < >= <= == != in crosses_above crosses_below.
Operand kinds (exactly one per operand):
  {{"const": number}} | {{"const_str": "text"}} | {{"field": name}} | {{"fundamental": name}} | {{"meta": name}} | {{"pattern": name}}
  {{"fn": NAME, "of": field, "period": n}} — optional "component" (MACD: line/signal/hist; BBANDS: upper/mid/lower; STOCH: k/d; ADX: adx/pdi/mdi), optional "params" (MACD fast/slow/signal, BBANDS std, SUPERTREND mult)
  {{"expr": "*", "args": [...]}} for arithmetic ({", ".join(sorted(EXPR_OPS))})
  Any operand may add "bars_ago": n.
Fields: {", ".join(sorted(FIELDS))}
Functions: {", ".join(sorted(FUNCTIONS))}
Patterns: {", ".join(sorted(PATTERNS))}
Fundamentals: {", ".join(sorted(FUNDAMENTALS))} (market_cap is in INR)
Meta: {", ".join(sorted(METAS))}

Example: "volume at least twice its 20 day average" ->
{{"timeframe": "1d", "left": {{"field": "volume"}}, "op": ">",
  "right": {{"expr": "*", "args": [{{"const": 2}}, {{"fn": "SMA", "of": "volume", "period": 20}}]}}}}"""


def _invoke(messages: list) -> str:
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=os.environ.get("SCANNER_NL_MODEL", "gpt-5.4-mini"),
                     temperature=0)
    return llm.invoke(messages).content


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


def generate_definition(prompt: str) -> tuple[dict, str]:
    messages = [("system", _SYSTEM), ("human", prompt)]
    last_error = ""
    for attempt in range(2):
        if attempt:
            messages.append(("human",
                             f"That definition was invalid: {last_error}. "
                             "Reply again with ONLY the corrected JSON object."))
        try:
            raw = _invoke(messages)
            payload = _extract_json(raw)
            parse_definition(payload["definition"])
            return payload["definition"], str(payload.get("explanation", ""))
        except Exception as exc:  # noqa: BLE001 — feed the error back for one retry
            last_error = str(exc)[:500]
            messages.append(("ai", raw if "raw" in dir() else ""))
    raise NlGenerationError(f"could not generate a valid definition: {last_error}")
```

Fix while implementing: the `messages.append(("ai", ...))` line uses a broken `dir()` check — write it plainly:

```python
        raw = ""
        try:
            raw = _invoke(messages)
            payload = _extract_json(raw)
            parse_definition(payload["definition"])
            return payload["definition"], str(payload.get("explanation", ""))
        except Exception as exc:  # noqa: BLE001 — feed the error back for one retry
            last_error = str(exc)[:500]
            messages.append(("ai", raw or "(no output)"))
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_nl.py -v` → 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/scanner/nl.py tests/test_scanner_nl.py
git commit -m "feat(scanner): NL-to-definition with validation retry"
```

---

### Task 12: App wiring

**Files:**
- Modify: `apps/api/app.py` (imports at top; router registration after line 115 `app.include_router(stream_router, ...)`; lifespan around line 66)
- Test: `tests/test_scanner_wiring.py`

**Interfaces:**
- Consumes: `scanners` router, `seed_prebuilt`, `seed_universe`, `ingest_loop`, `get_scanner_store`
- Produces: `/api/scanners*` live on the real app; ingest task starts unless `SCANNER_INGEST=0`

- [ ] **Step 1: Write the failing test**

`tests/test_scanner_wiring.py`:

```python
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.scanner.store import reset_scanner_store_for_tests


def test_scanner_routes_mounted_and_prebuilt_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_DB_PATH", str(tmp_path / "scanner.sqlite"))
    monkeypatch.setenv("SCANNER_INGEST", "0")
    monkeypatch.setenv("WEBAPP_DB_PATH", str(tmp_path / "runs.sqlite"))
    reset_scanner_store_for_tests(tmp_path / "scanner.sqlite")

    from apps.api.app import create_app
    with TestClient(create_app()) as client:
        r = client.get("/api/scanners")
        assert r.status_code == 200
        assert len([s for s in r.json() if s["prebuilt"]]) == 10
```

- [ ] **Step 2: Run to verify FAIL** — 404 on `/api/scanners`

- [ ] **Step 3: Wire it in**

In `apps/api/app.py`, add imports next to the existing route imports:

```python
from apps.api.routes.scanners import router as scanners_router
from apps.api.scanner.ingest import ingest_loop
from apps.api.scanner.seed import seed_prebuilt
from apps.api.scanner.store import get_scanner_store
```

Register the router with the others:

```python
    app.include_router(scanners_router, prefix="/api")
```

In the lifespan, after `get_runner()` and before `yield`, start seeding + ingest; cancel on shutdown:

```python
        scanner_task = None
        seed_prebuilt(get_scanner_store())
        if os.environ.get("SCANNER_INGEST", "1") != "0":
            scanner_task = asyncio.create_task(ingest_loop(), name="scanner-ingest")
        logger.info("webapp ready")
        try:
            yield
        finally:
            if scanner_task is not None:
                scanner_task.cancel()
            shutdown_runner()
```

(Replace the existing `logger.info("webapp ready") / try / yield / finally` block — don't duplicate it.)

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_scanner_wiring.py -v`, then the full scanner suite: `pytest tests/test_scanner_*.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/api/app.py tests/test_scanner_wiring.py
git commit -m "feat(scanner): mount routes, seed prebuilts, start ingest loop"
```

---

### Task 13: Frontend types + API client + rows⇄AST converters

**Files:**
- Create: `apps/web/src/lib/scanner-types.ts`, `apps/web/src/lib/scanner-rows.ts`
- Modify: `apps/web/src/lib/api.ts` (add methods to the exported `api` object, reusing its existing fetch/auth helper — same pattern as `api.getRun`)
- Test: `apps/web/src/lib/scanner-rows.test.ts`

**Interfaces:**
- Produces (consumed by Tasks 14–15):
  - types: `ScanOperand`, `ScanCondition`, `ScanGroup`, `ScannerSummary`, `ScanMatch`, `ScanResult`, `Timeframe`
  - api methods: `listScanners(): Promise<ScannerSummary[]>`, `createScanner(body)`, `updateScanner(id, body)`, `deleteScanner(id)`, `runScanner(id): Promise<ScanResult>`, `previewScanner(definition): Promise<ScanResult>`, `nlScanner(prompt): Promise<{definition: ScanGroup; explanation: string}>`
  - converters: `rowsToAst(state: BuilderState): ScanGroup`, `astToRows(def: ScanGroup): BuilderState | null` (null = too complex for the simple builder, show JSON tab), plus `BuilderState`, `Row`, `SimpleOperand`, `emptyRow()`, `FIELD_OPTIONS`, `FN_OPTIONS`, `OP_OPTIONS`, `TIMEFRAME_OPTIONS`

- [ ] **Step 1: Write the types**

`apps/web/src/lib/scanner-types.ts`:

```ts
export type Timeframe = '5m' | '15m' | '1h' | '1d' | '1w' | '1mo'

export type ScanOperand = {
  const?: number
  const_str?: string
  field?: string
  fn?: string
  of?: string | ScanOperand
  period?: number
  params?: Record<string, number>
  component?: string
  expr?: string
  args?: ScanOperand[]
  fundamental?: string
  meta?: string
  pattern?: string
  bars_ago?: number
}

export type ScanCondition = {
  timeframe: Timeframe
  left: ScanOperand
  op?: string
  right?: ScanOperand
  for_n_bars?: number
}

export type ScanGroup = {
  logic: 'AND' | 'OR'
  children: (ScanGroup | ScanCondition)[]
}

export type ScannerSummary = {
  id: string
  name: string
  description: string
  prebuilt: boolean
  definition: ScanGroup
  created_at: string
  updated_at: string
}

export type ScanMatch = {
  symbol: string
  name: string
  sector: string | null
  close: number | null
  change_pct: number | null
  volume: number | null
  rvol: number | null
  values: Record<string, number | null>
}

export type ScanResult = {
  data_as_of: string
  universe: number
  matches: ScanMatch[]
}
```

- [ ] **Step 2: Write the failing converter tests**

`apps/web/src/lib/scanner-rows.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { astToRows, emptyRow, rowsToAst } from './scanner-rows'
import type { ScanGroup } from './scanner-types'

const SIMPLE: ScanGroup = {
  logic: 'AND',
  children: [
    { timeframe: '1d', left: { fn: 'EMA', of: 'close', period: 20 },
      op: 'crosses_above', right: { fn: 'EMA', of: 'close', period: 50 } },
    { timeframe: '15m', left: { field: 'volume' }, op: '>',
      right: { expr: '*', args: [{ const: 2 }, { fn: 'SMA', of: 'volume', period: 20 }] } },
  ],
}

describe('rows <-> ast', () => {
  it('round-trips a simple definition', () => {
    const rows = astToRows(SIMPLE)
    expect(rows).not.toBeNull()
    expect(rowsToAst(rows!)).toEqual(SIMPLE)
  })

  it('round-trips one nested OR group', () => {
    const def: ScanGroup = {
      logic: 'AND',
      children: [
        SIMPLE.children[0],
        { logic: 'OR', children: [SIMPLE.children[1], SIMPLE.children[0]] },
      ],
    }
    const rows = astToRows(def)
    expect(rows).not.toBeNull()
    expect(rowsToAst(rows!)).toEqual(def)
  })

  it('returns null for shapes the simple builder cannot edit', () => {
    const deep: ScanGroup = {
      logic: 'AND',
      children: [{ logic: 'OR', children: [{ logic: 'AND', children: [SIMPLE.children[0]] }] }],
    }
    expect(astToRows(deep)).toBeNull()
    expect(astToRows({ logic: 'AND', children: [
      { timeframe: '1d', left: { pattern: 'doji' } }] })).toBeNull()
  })

  it('emptyRow produces a valid condition', () => {
    const ast = rowsToAst({ groups: [{ logic: 'AND', rows: [emptyRow()] }] })
    expect(ast.children.length).toBe(1)
  })
})
```

Run: `cd apps/web && npx vitest run src/lib/scanner-rows.test.ts` — Expected: FAIL (module missing)

- [ ] **Step 3: Implement the converters**

`apps/web/src/lib/scanner-rows.ts`:

```ts
/* Simple-builder row model: a flat list of groups (depth 2 max), each row one
   condition. Operands cover const / field / fn / multiplier*fn — anything
   richer (patterns, nested groups >2, fundamentals math) edits as raw JSON. */
import type { ScanCondition, ScanGroup, ScanOperand, Timeframe } from './scanner-types'

export type SimpleOperand =
  | { kind: 'const'; value: number }
  | { kind: 'field'; field: string }
  | { kind: 'fn'; fn: string; of: string; period?: number; component?: string; mult?: number }

export type Row = {
  timeframe: Timeframe
  left: SimpleOperand
  op: string
  right: SimpleOperand
  forN?: number
}

export type BuilderState = { groups: { logic: 'AND' | 'OR'; rows: Row[] }[] }

export const TIMEFRAME_OPTIONS: Timeframe[] = ['5m', '15m', '1h', '1d', '1w', '1mo']
export const OP_OPTIONS = ['>', '<', '>=', '<=', '==', 'crosses_above', 'crosses_below']
export const FIELD_OPTIONS = ['open', 'high', 'low', 'close', 'volume', 'vwap',
  'typical_price', 'gap_pct', 'change_pct', 'body', 'upper_wick', 'lower_wick']
export const FN_OPTIONS = ['SMA', 'EMA', 'WMA', 'HMA', 'VWMA', 'RSI', 'STOCH', 'STOCHRSI',
  'CCI', 'WILLR', 'ROC', 'MOM', 'MACD', 'ADX', 'SUPERTREND', 'PSAR', 'ATR', 'BBANDS',
  'BBWIDTH', 'STDDEV', 'OBV', 'MFI', 'CMF', 'HIGHEST', 'LOWEST', 'SUM', 'AVG']

export function emptyRow(): Row {
  return {
    timeframe: '1d',
    left: { kind: 'field', field: 'close' },
    op: '>',
    right: { kind: 'const', value: 100 },
  }
}

function operandToAst(o: SimpleOperand): ScanOperand {
  if (o.kind === 'const') return { const: o.value }
  if (o.kind === 'field') return { field: o.field }
  const fn: ScanOperand = { fn: o.fn, of: o.of, period: o.period }
  if (o.component) fn.component = o.component
  if (o.period === undefined) delete fn.period
  if (o.mult !== undefined && o.mult !== 1)
    return { expr: '*', args: [{ const: o.mult }, fn] }
  return fn
}

function operandFromAst(o: ScanOperand): SimpleOperand | null {
  if (o.bars_ago) return null
  if (o.const !== undefined) return { kind: 'const', value: o.const }
  if (o.field !== undefined) return { kind: 'field', field: o.field }
  if (o.fn !== undefined) {
    if (o.of !== undefined && typeof o.of !== 'string') return null
    const out: SimpleOperand = { kind: 'fn', fn: o.fn, of: (o.of as string) ?? 'close' }
    if (o.period !== undefined) out.period = o.period
    if (o.component !== undefined) out.component = o.component
    if (o.params && Object.keys(o.params).length) return null
    return out
  }
  if (o.expr === '*' && o.args?.length === 2 && o.args[0].const !== undefined) {
    const inner = operandFromAst(o.args[1])
    if (inner?.kind === 'fn') return { ...inner, mult: o.args[0].const }
  }
  return null
}

function conditionToAst(r: Row): ScanCondition {
  const c: ScanCondition = {
    timeframe: r.timeframe,
    left: operandToAst(r.left),
    op: r.op,
    right: operandToAst(r.right),
  }
  if (r.forN) c.for_n_bars = r.forN
  return c
}

function conditionFromAst(c: ScanCondition): Row | null {
  if (!c.op || !c.right) return null // pattern conditions → JSON tab
  const left = operandFromAst(c.left)
  const right = operandFromAst(c.right)
  if (!left || !right) return null
  const row: Row = { timeframe: c.timeframe, left, op: c.op, right }
  if (c.for_n_bars) row.forN = c.for_n_bars
  return row
}

export function rowsToAst(state: BuilderState): ScanGroup {
  const groups = state.groups.filter((g) => g.rows.length)
  if (groups.length === 1)
    return { logic: groups[0].logic, children: groups[0].rows.map(conditionToAst) }
  return {
    logic: 'AND',
    children: groups.map((g) =>
      g.rows.length === 1
        ? conditionToAst(g.rows[0])
        : { logic: g.logic, children: g.rows.map(conditionToAst) }),
  }
}

export function astToRows(def: ScanGroup): BuilderState | null {
  const groups: BuilderState['groups'] = []
  const top: Row[] = []
  for (const child of def.children) {
    if ('logic' in child) {
      if (def.logic !== 'AND') return null
      const rows: Row[] = []
      for (const inner of child.children) {
        if ('logic' in inner) return null // depth > 2
        const row = conditionFromAst(inner)
        if (!row) return null
        rows.push(row)
      }
      groups.push({ logic: child.logic, rows })
    } else {
      const row = conditionFromAst(child)
      if (!row) return null
      top.push(row)
    }
  }
  if (top.length) groups.unshift({ logic: def.logic, rows: top })
  if (!groups.length) return null
  return { groups }
}
```

Note the round-trip subtlety: `rowsToAst(astToRows(x))` must equal `x` for the test's shapes. A single-condition nested group re-emits as a bare condition — the second test uses a 2-row OR group so it survives. If the first test fails on key ordering, compare with `toEqual` (structural) — already the case.

- [ ] **Step 4: Run to verify PASS** — `cd apps/web && npx vitest run src/lib/scanner-rows.test.ts` → 4 PASS

- [ ] **Step 5: Add API client methods**

In `apps/web/src/lib/api.ts`, import the new types and add to the exported `api` object, following the exact fetch/auth pattern of the existing methods (e.g. `getRun`):

```ts
import type { ScanGroup, ScanResult, ScannerSummary } from './scanner-types'

// inside the `api` object:
  listScanners: () => request<ScannerSummary[]>('/scanners'),
  createScanner: (body: { name: string; description?: string; definition: ScanGroup }) =>
    request<ScannerSummary>('/scanners', { method: 'POST', body: JSON.stringify(body) }),
  updateScanner: (id: string, body: { name: string; description?: string; definition: ScanGroup }) =>
    request<ScannerSummary>(`/scanners/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteScanner: (id: string) => request<void>(`/scanners/${id}`, { method: 'DELETE' }),
  runScanner: (id: string) => request<ScanResult>(`/scanners/${id}/run`, { method: 'POST' }),
  previewScanner: (definition: ScanGroup) =>
    request<ScanResult>('/scanners/preview', { method: 'POST', body: JSON.stringify({ definition }) }),
  nlScanner: (prompt: string) =>
    request<{ definition: ScanGroup; explanation: string }>('/scanners/nl',
      { method: 'POST', body: JSON.stringify({ prompt }) }),
```

`request` here stands for api.ts's existing internal helper (the one `getRun` uses — it attaches auth headers and throws `ApiError`); use its real name and signature, including any JSON `Content-Type` handling the existing POST methods do.

- [ ] **Step 6: Typecheck and commit**

Run: `cd apps/web && npx tsc --noEmit` (or the repo's `npm run check` script if present) — Expected: clean.

```bash
git add apps/web/src/lib/scanner-types.ts apps/web/src/lib/scanner-rows.ts apps/web/src/lib/scanner-rows.test.ts apps/web/src/lib/api.ts
git commit -m "feat(web/scanner): types, api client, builder row converters"
```

---

### Task 14: Results table + scanners list page

**Files:**
- Create: `apps/web/src/components/scanner/ResultsTable.tsx`, `apps/web/src/routes/scanners.index.tsx`
- Modify: none (shadcn `table` component added via CLI)

**Interfaces:**
- Consumes: `api.listScanners/runScanner`, `ScanResult`, shadcn ui components, `Topbar` from `#/components/shared/Topbar`
- Produces: `<ResultsTable result={ScanResult} />` (sortable columns, TradingView dialog on row click) — reused by the builder page in Task 15

- [ ] **Step 1: Install the shadcn table component**

```bash
cd apps/web && npx shadcn@latest add table
```

- [ ] **Step 2: Implement ResultsTable**

`apps/web/src/components/scanner/ResultsTable.tsx`:

```tsx
import { useMemo, useState } from 'react'
import { Dialog, DialogContent, DialogTitle } from '#/components/ui/dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '#/components/ui/table'
import type { ScanMatch, ScanResult } from '#/lib/scanner-types'

const BASE_COLS: { key: keyof ScanMatch; label: string }[] = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'name', label: 'Name' },
  { key: 'sector', label: 'Sector' },
  { key: 'close', label: 'Price' },
  { key: 'change_pct', label: '% Chg' },
  { key: 'volume', label: 'Volume' },
  { key: 'rvol', label: 'RVol' },
]

export function ResultsTable({ result }: { result: ScanResult }) {
  const [sortKey, setSortKey] = useState<string>('change_pct')
  const [desc, setDesc] = useState(true)
  const [chartSymbol, setChartSymbol] = useState<string | null>(null)

  const valueCols = useMemo(
    () => Object.keys(result.matches[0]?.values ?? {}),
    [result],
  )

  const rows = useMemo(() => {
    const get = (m: ScanMatch) =>
      (BASE_COLS.some((c) => c.key === sortKey)
        ? m[sortKey as keyof ScanMatch]
        : m.values[sortKey]) as number | string | null
    return [...result.matches].sort((a, b) => {
      const av = get(a), bv = get(b)
      if (av == null) return 1
      if (bv == null) return -1
      const cmp = typeof av === 'string' ? av.localeCompare(String(bv)) : Number(av) - Number(bv)
      return desc ? -cmp : cmp
    })
  }, [result, sortKey, desc])

  const onSort = (key: string) => {
    if (key === sortKey) setDesc(!desc)
    else { setSortKey(key); setDesc(true) }
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">
        {result.matches.length} of {result.universe} stocks · data as of{' '}
        {result.data_as_of ? new Date(result.data_as_of).toLocaleString() : '—'} (delayed)
      </p>
      <div className="overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {[...BASE_COLS.map((c) => ({ key: String(c.key), label: c.label })),
                ...valueCols.map((k) => ({ key: k, label: k }))].map((c) => (
                <TableHead key={c.key} onClick={() => onSort(c.key)}
                  className="cursor-pointer select-none whitespace-nowrap">
                  {c.label}{sortKey === c.key ? (desc ? ' ↓' : ' ↑') : ''}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((m) => (
              <TableRow key={m.symbol} className="cursor-pointer"
                onClick={() => setChartSymbol(m.symbol)}>
                <TableCell className="font-medium">{m.symbol}</TableCell>
                <TableCell className="max-w-48 truncate">{m.name}</TableCell>
                <TableCell>{m.sector ?? '—'}</TableCell>
                <TableCell>{m.close?.toLocaleString() ?? '—'}</TableCell>
                <TableCell className={m.change_pct != null && m.change_pct < 0
                  ? 'text-red-500' : 'text-emerald-500'}>
                  {m.change_pct != null ? `${m.change_pct.toFixed(2)}%` : '—'}
                </TableCell>
                <TableCell>{m.volume?.toLocaleString() ?? '—'}</TableCell>
                <TableCell>{m.rvol?.toFixed(2) ?? '—'}</TableCell>
                {valueCols.map((k) => (
                  <TableCell key={k}>{m.values[k]?.toFixed(2) ?? '—'}</TableCell>
                ))}
              </TableRow>
            ))}
            {!rows.length && (
              <TableRow><TableCell colSpan={7 + valueCols.length}
                className="py-8 text-center text-muted-foreground">
                No stocks match right now.
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={!!chartSymbol} onOpenChange={(o) => !o && setChartSymbol(null)}>
        <DialogContent className="max-w-4xl">
          <DialogTitle>{chartSymbol} — live chart (TradingView)</DialogTitle>
          {chartSymbol && (
            <iframe
              title={`chart-${chartSymbol}`}
              src={`https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(`NSE:${chartSymbol}`)}&interval=D&hidesidetoolbar=1&theme=dark&style=1&locale=en`}
              className="h-[480px] w-full rounded-md border-0"
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
```

- [ ] **Step 3: Implement the list page**

`apps/web/src/routes/scanners.index.tsx`:

```tsx
import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Topbar } from '#/components/shared/Topbar'
import { Badge } from '#/components/ui/badge'
import { Button } from '#/components/ui/button'
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from '#/components/ui/card'
import { api, getAuthToken } from '#/lib/api'
import type { ScanResult, ScannerSummary } from '#/lib/scanner-types'

export const Route = createFileRoute('/scanners/')({
  loader: async () => {
    await getAuthToken()
    return { scanners: await api.listScanners() }
  },
  component: ScannersPage,
})

function ScannersPage() {
  const { scanners } = Route.useLoaderData()
  const [items, setItems] = useState<ScannerSummary[]>(scanners)
  const [running, setRunning] = useState<string | null>(null)
  const [result, setResult] = useState<{ name: string; data: ScanResult } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run(s: ScannerSummary) {
    setRunning(s.id); setError(null)
    try {
      setResult({ name: s.name, data: await api.runScanner(s.id) })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(null)
    }
  }

  async function remove(s: ScannerSummary) {
    await api.deleteScanner(s.id)
    setItems(items.filter((i) => i.id !== s.id))
  }

  const prebuilt = items.filter((s) => s.prebuilt)
  const mine = items.filter((s) => !s.prebuilt)

  const section = (title: string, list: ScannerSummary[], editable: boolean) => (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">{title}</h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {list.map((s) => (
          <Card key={s.id}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                {s.name}
                {s.prebuilt && <Badge variant="secondary">prebuilt</Badge>}
              </CardTitle>
              <CardDescription>{s.description}</CardDescription>
            </CardHeader>
            <CardContent className="flex gap-2">
              <Button size="sm" disabled={running === s.id} onClick={() => run(s)}>
                {running === s.id ? 'Scanning…' : 'Run'}
              </Button>
              {editable && (
                <>
                  <Button size="sm" variant="outline" asChild>
                    <Link to="/scanners/$id/edit" params={{ id: s.id }}>Edit</Link>
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => remove(s)}>Delete</Button>
                </>
              )}
            </CardContent>
          </Card>
        ))}
        {!list.length && <p className="text-sm text-muted-foreground">None yet.</p>}
      </div>
    </section>
  )

  return (
    <div className="min-h-screen">
      <Topbar />
      <main className="mx-auto max-w-6xl space-y-8 p-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">Scanners</h1>
          <Button asChild><Link to="/scanners/new">New scanner</Link></Button>
        </div>
        {section('Prebuilt', prebuilt, false)}
        {section('My scanners', mine, true)}
        {error && <p className="text-sm text-red-500">{error}</p>}
        {result && (
          <section className="space-y-2">
            <h2 className="text-lg font-semibold">Results — {result.name}</h2>
            <ResultsTable result={result.data} />
          </section>
        )}
      </main>
    </div>
  )
}
```

If `Topbar` requires props (check its signature), pass what the other routes pass.

- [ ] **Step 4: Verify**

Run: `cd apps/web && npx tsc --noEmit && npm run build` — Expected: clean build. Then manually: start the dev stack (`docker-compose.dev.yml` or native per `DEV.md`) with `SCANNER_INGEST=0`, hit `/scanners`, confirm prebuilt cards render and Run returns a (possibly empty) table.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/scanner/ apps/web/src/routes/scanners.index.tsx apps/web/src/components/ui/table.tsx
git commit -m "feat(web/scanner): scanners list page with results table + TradingView dialog"
```

---

### Task 15: Builder page + NL box

**Files:**
- Create: `apps/web/src/components/scanner/ScannerBuilder.tsx`, `apps/web/src/routes/scanners.new.tsx`, `apps/web/src/routes/scanners.$id.edit.tsx`

**Interfaces:**
- Consumes: everything from Tasks 13–14
- Produces: `<ScannerBuilder initial={ScannerSummary | null} />` — full create/edit flow

- [ ] **Step 1: Implement the builder component**

`apps/web/src/components/scanner/ScannerBuilder.tsx`:

```tsx
import { useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Button } from '#/components/ui/button'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '#/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '#/components/ui/tabs'
import { Textarea } from '#/components/ui/textarea'
import { api } from '#/lib/api'
import {
  astToRows, emptyRow, FIELD_OPTIONS, FN_OPTIONS, OP_OPTIONS, rowsToAst,
  TIMEFRAME_OPTIONS, type BuilderState, type Row, type SimpleOperand,
} from '#/lib/scanner-rows'
import type { ScanGroup, ScanResult, ScannerSummary } from '#/lib/scanner-types'

function OperandEditor({ value, onChange, allowMult }: {
  value: SimpleOperand
  onChange: (o: SimpleOperand) => void
  allowMult?: boolean
}) {
  return (
    <div className="flex items-center gap-1">
      <Select value={value.kind} onValueChange={(kind) => onChange(
        kind === 'const' ? { kind: 'const', value: 100 }
          : kind === 'field' ? { kind: 'field', field: 'close' }
            : { kind: 'fn', fn: 'SMA', of: 'close', period: 20 })}>
        <SelectTrigger className="w-24"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="const">Number</SelectItem>
          <SelectItem value="field">Price/Vol</SelectItem>
          <SelectItem value="fn">Indicator</SelectItem>
        </SelectContent>
      </Select>
      {value.kind === 'const' && (
        <Input type="number" className="w-24" value={value.value}
          onChange={(e) => onChange({ kind: 'const', value: Number(e.target.value) })} />
      )}
      {value.kind === 'field' && (
        <Select value={value.field} onValueChange={(field) => onChange({ kind: 'field', field })}>
          <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
          <SelectContent>{FIELD_OPTIONS.map((f) =>
            <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
        </Select>
      )}
      {value.kind === 'fn' && (
        <>
          {allowMult && (
            <Input type="number" step="0.1" className="w-16" placeholder="1x"
              value={value.mult ?? ''} onChange={(e) => onChange({
                ...value, mult: e.target.value ? Number(e.target.value) : undefined })} />
          )}
          <Select value={value.fn} onValueChange={(fn) => onChange({ ...value, fn })}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>{FN_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Select value={value.of} onValueChange={(of) => onChange({ ...value, of })}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>{FIELD_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Input type="number" className="w-20" placeholder="period"
            value={value.period ?? ''} onChange={(e) => onChange({
              ...value, period: e.target.value ? Number(e.target.value) : undefined })} />
        </>
      )}
    </div>
  )
}

export function ScannerBuilder({ initial }: { initial: ScannerSummary | null }) {
  const navigate = useNavigate()
  const initialRows = initial ? astToRows(initial.definition) : { groups: [{ logic: 'AND' as const, rows: [emptyRow()] }] }
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [state, setState] = useState<BuilderState | null>(initialRows)
  const [json, setJson] = useState(() =>
    JSON.stringify(initial?.definition ?? rowsToAst(initialRows!), null, 2))
  const [tab, setTab] = useState(initialRows ? 'builder' : 'json')
  const [nlPrompt, setNlPrompt] = useState('')
  const [nlBusy, setNlBusy] = useState(false)
  const [explanation, setExplanation] = useState<string | null>(null)
  const [preview, setPreview] = useState<ScanResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function currentDefinition(): ScanGroup {
    if (tab === 'builder' && state) return rowsToAst(state)
    return JSON.parse(json) as ScanGroup
  }

  function setDefinition(def: ScanGroup) {
    const rows = astToRows(def)
    setState(rows)
    setJson(JSON.stringify(def, null, 2))
    setTab(rows ? 'builder' : 'json')
  }

  async function generate() {
    setNlBusy(true); setError(null)
    try {
      const { definition, explanation } = await api.nlScanner(nlPrompt)
      setDefinition(definition)
      setExplanation(explanation)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setNlBusy(false)
    }
  }

  async function runPreview() {
    setBusy(true); setError(null)
    try {
      setPreview(await api.previewScanner(currentDefinition()))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true); setError(null)
    try {
      const body = { name, description, definition: currentDefinition() }
      if (initial) await api.updateScanner(initial.id, body)
      else await api.createScanner(body)
      navigate({ to: '/scanners' })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const updateRow = (gi: number, ri: number, row: Row) => {
    const next = structuredClone(state!)
    next.groups[gi].rows[ri] = row
    setState(next)
  }

  return (
    <div className="space-y-6">
      {/* NL box */}
      <div className="space-y-2 rounded-md border p-4">
        <Label htmlFor="nl">Describe your scan</Label>
        <div className="flex gap-2">
          <Textarea id="nl" value={nlPrompt} onChange={(e) => setNlPrompt(e.target.value)}
            placeholder="e.g. 20 EMA crosses above 50 EMA, RSI above 60, volume twice the 20-day average"
            className="min-h-16" />
          <Button onClick={generate} disabled={nlBusy || !nlPrompt.trim()}>
            {nlBusy ? 'Generating…' : 'Generate'}
          </Button>
        </div>
        {explanation && <p className="text-sm text-muted-foreground">
          Generated: {explanation} Review the conditions below before running.</p>}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div><Label htmlFor="name">Name</Label>
          <Input id="name" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div><Label htmlFor="desc">Description</Label>
          <Input id="desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="builder" disabled={!state}>Builder</TabsTrigger>
          <TabsTrigger value="json">JSON</TabsTrigger>
        </TabsList>
        <TabsContent value="builder" className="space-y-4">
          {state?.groups.map((g, gi) => (
            <div key={gi} className="space-y-2 rounded-md border p-3">
              <div className="flex items-center gap-2">
                <Select value={g.logic} onValueChange={(logic) => {
                  const next = structuredClone(state)
                  next.groups[gi].logic = logic as 'AND' | 'OR'
                  setState(next)
                }}>
                  <SelectTrigger className="w-20"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="AND">AND</SelectItem>
                    <SelectItem value="OR">OR</SelectItem>
                  </SelectContent>
                </Select>
                <span className="text-sm text-muted-foreground">group {gi + 1}</span>
                {state.groups.length > 1 && (
                  <Button size="sm" variant="ghost" onClick={() => {
                    const next = structuredClone(state)
                    next.groups.splice(gi, 1)
                    setState(next)
                  }}>Remove group</Button>
                )}
              </div>
              {g.rows.map((r, ri) => (
                <div key={ri} className="flex flex-wrap items-center gap-2">
                  <Select value={r.timeframe} onValueChange={(timeframe) =>
                    updateRow(gi, ri, { ...r, timeframe: timeframe as Row['timeframe'] })}>
                    <SelectTrigger className="w-20"><SelectValue /></SelectTrigger>
                    <SelectContent>{TIMEFRAME_OPTIONS.map((t) =>
                      <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
                  </Select>
                  <OperandEditor value={r.left} onChange={(left) => updateRow(gi, ri, { ...r, left })} />
                  <Select value={r.op} onValueChange={(op) => updateRow(gi, ri, { ...r, op })}>
                    <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
                    <SelectContent>{OP_OPTIONS.map((o) =>
                      <SelectItem key={o} value={o}>{o.replace('_', ' ')}</SelectItem>)}</SelectContent>
                  </Select>
                  <OperandEditor allowMult value={r.right}
                    onChange={(right) => updateRow(gi, ri, { ...r, right })} />
                  <Button size="sm" variant="ghost" onClick={() => {
                    const next = structuredClone(state)
                    next.groups[gi].rows.splice(ri, 1)
                    setState(next)
                  }}>✕</Button>
                </div>
              ))}
              <Button size="sm" variant="outline" onClick={() => {
                const next = structuredClone(state)
                next.groups[gi].rows.push(emptyRow())
                setState(next)
              }}>Add condition</Button>
            </div>
          ))}
          <Button variant="outline" onClick={() => {
            const next = structuredClone(state!)
            next.groups.push({ logic: 'AND', rows: [emptyRow()] })
            setState(next)
          }}>Add group</Button>
        </TabsContent>
        <TabsContent value="json">
          <Textarea value={json} onChange={(e) => setJson(e.target.value)}
            className="min-h-64 font-mono text-xs" />
        </TabsContent>
      </Tabs>

      <div className="flex gap-2">
        <Button variant="outline" onClick={runPreview} disabled={busy}>
          {busy ? 'Scanning…' : 'Preview results'}
        </Button>
        <Button onClick={save} disabled={busy || !name.trim()}>
          {initial ? 'Save changes' : 'Create scanner'}
        </Button>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
      {preview && <ResultsTable result={preview} />}
    </div>
  )
}
```

Sync note: when switching from the JSON tab to the builder tab after hand-editing JSON, the builder shows stale rows. Handle it in `onValueChange` of the Tabs: when moving to `builder`, try `setDefinition(JSON.parse(json))` inside a try/catch and stay on `json` with an error message on parse failure. Add that in this step — it's four lines.

- [ ] **Step 2: Create the route pages**

`apps/web/src/routes/scanners.new.tsx`:

```tsx
import { createFileRoute } from '@tanstack/react-router'
import { ScannerBuilder } from '#/components/scanner/ScannerBuilder'
import { Topbar } from '#/components/shared/Topbar'

export const Route = createFileRoute('/scanners/new')({
  component: () => (
    <div className="min-h-screen">
      <Topbar />
      <main className="mx-auto max-w-5xl space-y-4 p-4">
        <h1 className="text-2xl font-bold">New scanner</h1>
        <ScannerBuilder initial={null} />
      </main>
    </div>
  ),
})
```

`apps/web/src/routes/scanners.$id.edit.tsx`:

```tsx
import { createFileRoute } from '@tanstack/react-router'
import { ScannerBuilder } from '#/components/scanner/ScannerBuilder'
import { Topbar } from '#/components/shared/Topbar'
import { api, getAuthToken } from '#/lib/api'

export const Route = createFileRoute('/scanners/$id/edit')({
  loader: async ({ params }) => {
    await getAuthToken()
    const scanners = await api.listScanners()
    const scanner = scanners.find((s) => s.id === params.id)
    if (!scanner || scanner.prebuilt) throw new Error('scanner not found')
    return { scanner }
  },
  component: EditPage,
})

function EditPage() {
  const { scanner } = Route.useLoaderData()
  return (
    <div className="min-h-screen">
      <Topbar />
      <main className="mx-auto max-w-5xl space-y-4 p-4">
        <h1 className="text-2xl font-bold">Edit — {scanner.name}</h1>
        <ScannerBuilder initial={scanner} />
      </main>
    </div>
  )
}
```

- [ ] **Step 3: Verify**

`cd apps/web && npx vitest run && npx tsc --noEmit && npm run build` — all clean.
Manual pass with the dev stack: create a scanner via builder, via NL, via JSON tab; preview; save; edit; delete; run a prebuilt; click a result row → TradingView chart.

- [ ] **Step 4: Full-suite check**

Run: `pytest tests/test_scanner_*.py -v` (repo root) — all green.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/scanner/ScannerBuilder.tsx apps/web/src/routes/scanners.new.tsx 'apps/web/src/routes/scanners.$id.edit.tsx'
git commit -m "feat(web/scanner): builder page with NL generation, JSON tab, preview"
```

---

## Self-Review (done at plan-writing time)

1. **Spec coverage:** data layer (T1, T7, T8), engine + limits + missing-data rule (T2, T4, T5, T6), weekly/monthly resample (T6), API + ownership (T10), prebuilts (T9), NL with review-not-run (T11, T15), UI with delayed-data header + TradingView live chart (T14), builder 2-level groups + JSON escape hatch (T15), calendar (T3), wiring (T12). Deferred items (alerts, backtesting, realtime, ranking, order-book, Ichimoku) intentionally absent.
2. **Placeholders:** none — two known-rough code spots are called out explicitly with their corrected versions inline (Task 4 BBANDS branch, Task 11 retry-append; Task 6 first test's tmp_path).
3. **Type consistency:** `ScannerStore` method names match across T1/T6/T7/T8/T10; `parse_definition`/`DefinitionError` across T2/T9/T10/T11; `Panel`/`eval_operand`/`describe` across T4/T5/T6; TS `ScanGroup`/`ScanResult`/`api.*` across T13/T14/T15.

## Post-implementation notes

- First real deploy: initial backfill (~500 symbols × 4 timeframes) takes several minutes on startup; the API is usable immediately, scans fill in as bars land.
- `SCANNER_INGEST=0` in CI/tests and any environment that shouldn't hit Yahoo.
- Update `FORK_PATCHES.md` is NOT needed — no upstream file is touched.
- Deploy checklist: add `SCANNER_DB_PATH` to the mounted volume in the dokploy compose; optionally `SCANNER_NL_MODEL`.



