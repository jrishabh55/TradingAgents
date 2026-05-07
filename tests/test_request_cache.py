"""Cache lookup + canonicalization for POST /api/runs.

Covers the full chain:
  request → canonicalize_request → request_hash → JobStore.find_cached_run

End-to-end HTTP behaviour (including ``?force=true`` and the legacy "active
run for ticker" gate) is exercised in a separate FastAPI TestClient suite.
"""
from __future__ import annotations

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
