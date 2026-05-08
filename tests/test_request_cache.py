"""Cache lookup + canonicalization for POST /api/runs.

Covers the full chain:
  request → canonicalize_request → request_hash → JobStore.find_cached_run

End-to-end HTTP behaviour (including ``?force=true`` and the legacy "active
run for ticker" gate) is exercised in a separate FastAPI TestClient suite.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from apps.api.jobs.store import JobStore
from apps.api.schemas import RunRequest, canonicalize_request, request_hash


def _request(**overrides) -> RunRequest:
    base = dict(
        ticker="AAPL",
        analysis_date="2026-05-01",
        analysts=["market", "news"],
        research_depth=2,
        llm_provider="openai",
        backend_url=None,
        shallow_thinker="gpt-4o-mini",
        deep_thinker="gpt-4o",
        google_thinking_level=None,
        openai_reasoning_effort=None,
        anthropic_effort=None,
        output_language="English",
        checkpoint_enabled=False,
    )
    base.update(overrides)
    return RunRequest(**base)


# ---------- canonicalize_request ----------


def test_canonicalize_drops_checkpoint_enabled():
    """checkpoint_enabled is internal state and shouldn't change the cache key."""
    a = canonicalize_request(_request(checkpoint_enabled=False))
    b = canonicalize_request(_request(checkpoint_enabled=True))
    assert a == b
    assert "checkpoint_enabled" not in a


def test_canonicalize_sorts_analysts():
    """Analyst order is cosmetic — [news, market] must hash same as [market, news]."""
    a = canonicalize_request(_request(analysts=["news", "market"]))
    b = canonicalize_request(_request(analysts=["market", "news"]))
    assert a == b
    assert a["analysts"] == ["market", "news"]


def test_canonicalize_lowercases_provider():
    """OpenAI / openai / OPENAI all collide — provider names are case-insensitive."""
    a = canonicalize_request(_request(llm_provider="OpenAI"))
    b = canonicalize_request(_request(llm_provider="openai"))
    c = canonicalize_request(_request(llm_provider="OPENAI"))
    assert a == b == c


# ---------- request_hash ----------


def test_hash_stable_across_equivalent_requests():
    """Two semantically identical requests must produce the same hex digest."""
    h1 = request_hash(_request(analysts=["news", "market"], llm_provider="OpenAI"))
    h2 = request_hash(_request(analysts=["market", "news"], llm_provider="openai"))
    assert h1 == h2


@pytest.mark.parametrize(
    "field, alt",
    [
        ("ticker", "TSLA"),
        ("analysis_date", "2026-05-02"),
        ("research_depth", 3),
        ("shallow_thinker", "gpt-4o"),
        ("deep_thinker", "gpt-4o-mini"),
        ("output_language", "Hindi"),
        ("openai_reasoning_effort", "high"),
        ("anthropic_effort", "minimal"),
    ],
)
def test_hash_changes_when_meaningful_field_changes(field, alt):
    """Every field that affects output must affect the hash."""
    base_hash = request_hash(_request())
    other_hash = request_hash(_request(**{field: alt}))
    assert base_hash != other_hash, (
        f"{field}={alt!r} produced the same hash — cache key is missing this field"
    )


# ---------- JobStore.find_cached_run ----------


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "test.sqlite")


def _create_completed(store: JobStore, *, hash_: str, **overrides) -> str:
    """Insert a completed run with the given request_hash."""
    req = _request(**overrides)
    run_id = store.create_run(
        ticker=req.ticker,
        analysis_date=req.analysis_date,
        config=req.model_dump(),
        request_hash=hash_,
    )
    store.mark_running(run_id)
    store.mark_completed(
        run_id,
        decision_text="HOLD",
        rating="Hold",
        final_state={"final_trade_decision": "HOLD"},
    )
    return run_id


def test_find_cached_run_hit(store: JobStore):
    h = request_hash(_request())
    run_id = _create_completed(store, hash_=h)
    cached = store.find_cached_run(h, ttl_seconds=3600)
    assert cached is not None
    assert cached.id == run_id
    assert cached.status == "completed"


def test_find_cached_run_miss_when_no_match(store: JobStore):
    """Hash that nothing in the DB matches → None."""
    _create_completed(store, hash_=request_hash(_request()))
    cached = store.find_cached_run("0" * 64, ttl_seconds=3600)
    assert cached is None


def test_find_cached_run_skips_non_completed(store: JobStore):
    """A queued or failed run with the matching hash must NOT satisfy a cache lookup."""
    h = request_hash(_request())
    # Insert a queued (not completed) run with the same hash.
    req = _request()
    store.create_run(
        ticker=req.ticker,
        analysis_date=req.analysis_date,
        config=req.model_dump(),
        request_hash=h,
    )
    cached = store.find_cached_run(h, ttl_seconds=3600)
    assert cached is None


def test_find_cached_run_respects_ttl(store: JobStore):
    """A completed run finished older than TTL is not a hit."""
    h = request_hash(_request())
    _create_completed(store, hash_=h)
    # Sleep past TTL=0 so finished_at is strictly older than "now - 0s".
    time.sleep(0.05)
    cached = store.find_cached_run(h, ttl_seconds=0)
    assert cached is None


def test_find_cached_run_returns_most_recent(store: JobStore):
    """When multiple completed runs match the hash, return the latest."""
    h = request_hash(_request())
    older_id = _create_completed(store, hash_=h)
    time.sleep(0.05)
    newer_id = _create_completed(store, hash_=h)
    cached = store.find_cached_run(h, ttl_seconds=3600)
    assert cached is not None
    assert cached.id == newer_id
    assert cached.id != older_id


def test_init_schema_migrates_old_db(tmp_path: Path):
    """JobStore must boot on a DB created before request_hash/user_id existed.

    Regression for a real production-boot bug: the original schema script
    created indexes on (request_hash) AND CREATE TABLE IF NOT EXISTS, so on
    an existing pre-migration DB the table was untouched and the index
    creation hit "no such column: request_hash". Fix splits the schema into
    tables → migrations → indexes, ensuring columns exist before indexes
    reference them.
    """
    db_path = tmp_path / "old.sqlite"
    # Simulate a DB created on the original schema (no request_hash, no user_id).
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
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
            CREATE TABLE events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                seq       INTEGER NOT NULL,
                ts        TEXT NOT NULL,
                type      TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(run_id, seq)
            );
            INSERT INTO runs(id, ticker, analysis_date, status, created_at, config_json)
            VALUES ('legacy-1', 'AAPL', '2026-01-01', 'completed', '2026-01-01T00:00:00Z', '{}');
            """
        )

    # Constructing the store must not raise — this is the production path.
    store = JobStore(db_path)

    # New columns exist after migration.
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "request_hash" in cols
    assert "user_id" in cols

    # The legacy row is still there and queryable through the new code path.
    detail = store.get_run("legacy-1")
    assert detail is not None
    assert detail.user_id is None  # migrated row was untouched


def test_find_cached_run_user_scope(store: JobStore):
    """When user_id is supplied, only that user's runs are considered."""
    h = request_hash(_request())
    req = _request()
    # User A has a completed run with this hash.
    user_a_run = store.create_run(
        ticker=req.ticker,
        analysis_date=req.analysis_date,
        config=req.model_dump(),
        request_hash=h,
        user_id="user_a",
    )
    store.mark_running(user_a_run)
    store.mark_completed(
        user_a_run,
        decision_text="BUY",
        rating="Buy",
        final_state={"final_trade_decision": "BUY"},
    )

    # User B searching with the same hash gets nothing.
    cached_b = store.find_cached_run(h, ttl_seconds=3600, user_id="user_b")
    assert cached_b is None

    # User A finds their run.
    cached_a = store.find_cached_run(h, ttl_seconds=3600, user_id="user_a")
    assert cached_a is not None
    assert cached_a.id == user_a_run

    # Shared cache (no user filter) finds the run regardless.
    cached_shared = store.find_cached_run(h, ttl_seconds=3600)
    assert cached_shared is not None
    assert cached_shared.id == user_a_run
