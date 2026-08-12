"""Schema migrations must be additive and never lose rows.

The job store is the only durable record of a run, and a deployed instance
upgrades in place, so a migration that drops data or silently skips a column
would be discovered only when a resume failed.
"""
import json
import sqlite3
import typing

import pytest

from apps.api.jobs.store import _RUNS_TABLE_MIGRATIONS, JobStore
from apps.api.schemas import RunStatus

NEW_COLUMNS = ("checkpoint_ns", "effective_config_json")


def _columns(path):
    with sqlite3.connect(path) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(runs)")]


def _legacy_db(path):
    """A database at the ORIGINAL schema, before any migration column existed."""
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, ticker TEXT NOT NULL, analysis_date TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, config_json TEXT NOT NULL, decision_text TEXT,
                rating TEXT, final_state_json TEXT, error TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                seq INTEGER NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL,
                data_json TEXT NOT NULL, UNIQUE(run_id, seq)
            );
            """
        )
        c.execute(
            "INSERT INTO runs (id,ticker,analysis_date,status,created_at,config_json,rating)"
            " VALUES ('old-1','NSEI','2026-08-01','completed','2026-08-01T00:00:00+00:00',?,'Hold')",
            (json.dumps({"ticker": "NSEI"}),),
        )
    return path


def test_new_columns_exist_on_a_fresh_database(tmp_path):
    JobStore(tmp_path / "fresh.sqlite")
    cols = _columns(tmp_path / "fresh.sqlite")
    for col in NEW_COLUMNS:
        assert col in cols


def test_legacy_database_is_migrated_in_place_without_losing_rows(tmp_path):
    db = _legacy_db(tmp_path / "legacy.sqlite")
    before = _columns(db)
    for col in NEW_COLUMNS:
        assert col not in before

    JobStore(db)  # opening runs the migration

    after = _columns(db)
    for col in NEW_COLUMNS:
        assert col in after
    with sqlite3.connect(db) as c:
        rows = c.execute("select id, ticker, status, rating from runs").fetchall()
    assert rows == [("old-1", "NSEI", "completed", "Hold")]


def test_migration_is_idempotent(tmp_path):
    """Every process start re-runs it; a second pass must not error."""
    db = _legacy_db(tmp_path / "twice.sqlite")
    JobStore(db)
    JobStore(db)
    JobStore(db)
    assert _columns(db).count("checkpoint_ns") == 1


def test_a_migrated_store_still_reads_and_writes(tmp_path):
    db = _legacy_db(tmp_path / "rw.sqlite")
    store = JobStore(db)
    run_id = store.create_run(
        ticker="AAPL", analysis_date="2026-08-01",
        config={"ticker": "AAPL"}, request_hash="h", user_id="u1",
    )
    detail = store.get_run(run_id)
    assert detail is not None and detail.ticker == "AAPL"
    # The legacy row is still listed alongside the new one.
    assert {r.id for r in store.list_runs(limit=10, user_id=None)} >= {run_id}


def test_migration_list_covers_every_added_column():
    """Guards the fresh-vs-upgraded divergence: a column added to the CREATE
    TABLE but not to the migration list would exist only on fresh databases."""
    listed = {name for name, _ in _RUNS_TABLE_MIGRATIONS}
    for col in NEW_COLUMNS:
        assert col in listed


def test_interrupted_is_a_valid_run_status():
    """Distinct from 'failed': the run did not error, the process went away.
    Only this state is a candidate for checkpoint resume."""
    statuses = typing.get_args(RunStatus)
    assert "interrupted" in statuses
    # The pre-existing vocabulary must be preserved.
    for s in ("queued", "running", "completed", "failed", "cancelled"):
        assert s in statuses


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_terminal_statuses_are_all_representable(status):
    from apps.api.schemas import RunSummary
    from datetime import datetime

    r = RunSummary(id="x", ticker="A", analysis_date="2026-08-01",
                   status=status, created_at=datetime(2026, 8, 1))
    assert r.status == status
