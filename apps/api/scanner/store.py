"""SQLite persistence for the scanner: instruments, bars, scanner definitions.

Separate DB file from the runs store so bulk bar writes never contend with
run/SSE traffic. Same conventions as apps/api/jobs/store.py: WAL mode,
per-call connections.
"""
from __future__ import annotations

import contextlib
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

    @contextlib.contextmanager
    def _conn(self):
        # 15s busy timeout (matches jobs/store.py): bulk bar upserts from the
        # ingest loop can hold the write lock past sqlite's 5s default when a
        # second process (maintenance exec, tests) writes concurrently.
        conn = sqlite3.connect(self._path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- bars ------------------------------------------------------------
    def upsert_bars(self, timeframe: str, df: pd.DataFrame) -> None:
        rows = [(r.symbol, timeframe, r.ts, r.open, r.high, r.low, r.close, r.volume)
                for r in df.itertuples(index=False)]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO bars (symbol,timeframe,ts,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)

    def bump_bar_version(self) -> None:
        """Publish written bars to scan engines (they cache panels keyed on
        version()). Deliberately NOT part of upsert_bars: the ingest loop
        writes one chunk at a time across a multi-minute sweep, and a bump
        per chunk made every scan landing mid-sweep pay a full cold panel
        rebuild. Writers bump once per cycle, right before engine.warm()."""
        with self._conn() as c:
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
            df = pd.read_sql_query("SELECT symbol, yf_symbol, name, sector, industry, market_cap, "
                                    "index_memberships, fno, fundamentals_json FROM instruments", c)
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
