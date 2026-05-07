"""SQLite-backed persistence for runs and SSE events.

Threading model: every method acquires its own connection. SQLite handles
concurrent readers fine, and writes are serialized by a process-wide lock that
:mod:`sqlite3` manages internally. We use WAL mode so the SSE replay reader
doesn't block the worker writer.

Why SQLite (not Postgres / Redis): single-Docker constraint. One file in a
mounted volume survives container restarts and is trivial to back up.
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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apps.api.schemas import EventEnvelope, RunDetail, RunStatus, RunSummary


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    config_json   TEXT NOT NULL,
    decision_text TEXT,
    rating        TEXT,
    final_state_json TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_ticker_status ON runs(ticker, status);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    type      TEXT NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
"""


def _utcnow() -> str:
    """ISO 8601 UTC string with microseconds, sortable lexicographically."""
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class JobStore:
    """Thread-safe DAO over the SQLite job database."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # One lock guards schema init + the few multi-statement methods.
        # Per-call connections handle the rest of the concurrency.
        self._init_lock = threading.Lock()
        self._init_schema()

    # ---------- connection helpers ----------

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # autocommit mode; we use BEGIN/COMMIT explicitly when needed
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=15.0,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------- runs ----------

    def create_run(
        self,
        *,
        ticker: str,
        analysis_date: str,
        config: Dict[str, Any],
    ) -> str:
        """Insert a queued run and return its id."""
        run_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs(id, ticker, analysis_date, status, created_at, config_json) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (run_id, ticker, analysis_date, _utcnow(), json.dumps(config)),
            )
        return run_id

    def mark_running(self, run_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (_utcnow(), run_id),
            )

    def update_final_state(self, run_id: str, final_state: Dict[str, Any]) -> None:
        """Snapshot the in-progress final_state so /report.md returns partial data mid-run.

        Called from the runner after each graph chunk. Cheap because the JSON
        is at most a few hundred KB and SQLite WAL handles concurrent readers.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET final_state_json=? WHERE id=? AND status='running'",
                (json.dumps(final_state, default=str), run_id),
            )

    def mark_completed(
        self,
        run_id: str,
        *,
        decision_text: Optional[str],
        rating: Optional[str],
        final_state: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET status='completed', finished_at=?, decision_text=?, rating=?, "
                "final_state_json=? WHERE id=?",
                (_utcnow(), decision_text, rating, json.dumps(final_state, default=str), run_id),
            )

    def mark_failed(self, run_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?",
                (_utcnow(), error, run_id),
            )

    def mark_cancelled(self, run_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET status='cancelled', finished_at=? WHERE id=?",
                (_utcnow(), run_id),
            )

    def has_active_run_for_ticker(self, ticker: str) -> bool:
        """True if any run for ``ticker`` is queued or running.

        Used by the API to return HTTP 409 instead of letting two runs race the
        on-disk memory log (see the gotcha in the architecture plan).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE ticker=? AND status IN ('queued','running') LIMIT 1",
                (ticker,),
            ).fetchone()
            return row is not None

    def get_run(self, run_id: str) -> Optional[RunDetail]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return _row_to_detail(row)

    def list_runs(self, limit: int = 100) -> List[RunSummary]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, ticker, analysis_date, status, created_at, started_at, finished_at, "
                "rating, error FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            RunSummary(
                id=r["id"],
                ticker=r["ticker"],
                analysis_date=r["analysis_date"],
                status=r["status"],
                created_at=_parse_ts(r["created_at"]),
                started_at=_parse_ts(r["started_at"]),
                finished_at=_parse_ts(r["finished_at"]),
                rating=r["rating"],
                error=r["error"],
            )
            for r in rows
        ]

    # ---------- events ----------

    def append_event(
        self,
        run_id: str,
        *,
        seq: int,
        type: str,
        data: Dict[str, Any],
        ts: Optional[str] = None,
    ) -> EventEnvelope:
        """Persist one event row. ``seq`` is the SSE id for replay."""
        ts_str = ts or _utcnow()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events(run_id, seq, ts, type, data_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, ts_str, type, json.dumps(data, default=str)),
            )
        return EventEnvelope(seq=seq, ts=_parse_ts(ts_str), type=type, data=data)

    def replay_events(self, run_id: str, since_seq: int = 0) -> List[EventEnvelope]:
        """Return every persisted event for ``run_id`` with seq > since_seq."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, ts, type, data_json FROM events "
                "WHERE run_id=? AND seq>? ORDER BY seq ASC",
                (run_id, since_seq),
            ).fetchall()
        return [
            EventEnvelope(
                seq=r["seq"],
                ts=_parse_ts(r["ts"]),
                type=r["type"],
                data=json.loads(r["data_json"]),
            )
            for r in rows
        ]

    def latest_seq(self, run_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(seq) AS s FROM events WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row["s"] or 0)


def _row_to_detail(row: sqlite3.Row) -> RunDetail:
    config = json.loads(row["config_json"])
    final_state = json.loads(row["final_state_json"]) if row["final_state_json"] else {}
    return RunDetail(
        id=row["id"],
        ticker=row["ticker"],
        analysis_date=row["analysis_date"],
        status=row["status"],
        created_at=_parse_ts(row["created_at"]),
        started_at=_parse_ts(row["started_at"]),
        finished_at=_parse_ts(row["finished_at"]),
        rating=row["rating"],
        decision_text=row["decision_text"],
        error=row["error"],
        config=config,
        market_report=final_state.get("market_report"),
        sentiment_report=final_state.get("sentiment_report"),
        news_report=final_state.get("news_report"),
        fundamentals_report=final_state.get("fundamentals_report"),
        investment_plan=final_state.get("investment_plan"),
        trader_investment_plan=final_state.get("trader_investment_plan"),
        final_trade_decision=final_state.get("final_trade_decision"),
        investment_debate_state=final_state.get("investment_debate_state"),
        risk_debate_state=final_state.get("risk_debate_state"),
    )


# Module-level singleton. Resolved lazily so tests can override WEBAPP_DB_PATH.
_singleton: Optional[JobStore] = None
_singleton_lock = threading.Lock()


def get_store() -> JobStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                db_path = Path(
                    os.environ.get(
                        "WEBAPP_DB_PATH",
                        str(Path.home() / ".tradingagents" / "webapp.sqlite"),
                    )
                )
                _singleton = JobStore(db_path)
    return _singleton


def reset_store_for_tests(db_path: Path) -> JobStore:
    """Test helper: rebind the singleton to an explicit path."""
    global _singleton
    with _singleton_lock:
        _singleton = JobStore(db_path)
    return _singleton
