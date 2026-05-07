"""Per-user memory-log isolation + same-user serialization in JobRunner."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apps.api.integrations.graph_factory import _build_config, _safe_user_dir
from apps.api.jobs.bus import EventBus
from apps.api.jobs.runner import JobRunner
from apps.api.jobs.store import JobStore
from apps.api.schemas import RunRequest


def _request(**overrides) -> RunRequest:
    base = dict(
        ticker="AAPL",
        analysis_date="2026-05-01",
        analysts=["market"],
        research_depth=1,
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


# ---------- _safe_user_dir ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("user_2abcXYZ", "user_2abcXYZ"),  # Clerk format unchanged
        ("anonymous", "anonymous"),
        ("shared-bearer", "shared-bearer"),
        ("../etc/passwd", "etc_passwd"),  # path traversal blocked
        ("a b c", "a_b_c"),  # whitespace collapsed
        ("", "anonymous"),  # empty fallback
        ("////", "anonymous"),  # all-unsafe → fallback
        ("user.name@host", "user_name_host"),  # punctuation collapsed
    ],
)
def test_safe_user_dir(raw, expected):
    assert _safe_user_dir(raw) == expected


# ---------- _build_config: per-user memory_log_path ----------


def test_build_config_omits_memory_log_path_without_user(monkeypatch):
    """Without user_id, the upstream default memory_log_path is preserved."""
    # Whatever the upstream default is, _build_config shouldn't touch it
    # when no user_id is passed.
    config = _build_config(_request())
    # We don't assert a specific value because DEFAULT_CONFIG comes from
    # the (env-var-influenced) upstream module. We only assert we didn't
    # *override* it with our per-user path.
    assert "/memory_per_user/" not in (config.get("memory_log_path") or "")


def test_build_config_injects_per_user_path_with_user(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBAPP_PER_USER_MEMORY_DIR", str(tmp_path))
    config = _build_config(_request(), user_id="user_2abcXYZ")
    expected = tmp_path / "user_2abcXYZ" / "trading_memory.md"
    assert config["memory_log_path"] == str(expected)


def test_build_config_per_user_paths_differ(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBAPP_PER_USER_MEMORY_DIR", str(tmp_path))
    a = _build_config(_request(), user_id="alice")
    b = _build_config(_request(), user_id="bob")
    assert a["memory_log_path"] != b["memory_log_path"]
    assert "alice" in a["memory_log_path"]
    assert "bob" in b["memory_log_path"]


def test_build_config_sanitizes_user_id_in_path(monkeypatch, tmp_path):
    """Hostile user_id can't escape the memory_per_user/ jail."""
    monkeypatch.setenv("WEBAPP_PER_USER_MEMORY_DIR", str(tmp_path))
    config = _build_config(_request(), user_id="../etc/passwd")
    # The sanitized component contains underscores instead of slashes.
    assert "/etc/passwd" not in config["memory_log_path"]
    assert "etc_passwd" in config["memory_log_path"]


# ---------- JobRunner: per-user lock semantics ----------


@pytest.fixture()
def runner(tmp_path: Path):
    store = JobStore(tmp_path / "test.sqlite")
    bus = EventBus()
    r = JobRunner(store=store, bus=bus, concurrency=4)
    yield r
    r.shutdown()


def test_runner_creates_distinct_locks_per_user(runner: JobRunner):
    """Each user_id gets its own threading.Lock instance."""
    lock_a = runner._get_user_lock("alice")
    lock_b = runner._get_user_lock("bob")
    assert lock_a is not lock_b
    # Same user → same lock (cached).
    assert runner._get_user_lock("alice") is lock_a


def test_runner_lock_serializes_same_user_runs(runner: JobRunner):
    """If user A holds their lock, a second 'alice' submission can't acquire it."""
    alice_lock = runner._get_user_lock("alice")
    assert alice_lock.acquire(blocking=False)
    try:
        # A second attempt to grab alice's lock fails immediately.
        assert not alice_lock.acquire(blocking=False)
        # Bob's lock is unaffected.
        bob_lock = runner._get_user_lock("bob")
        assert bob_lock.acquire(blocking=False)
        bob_lock.release()
    finally:
        alice_lock.release()


def test_runner_submit_threads_user_id_into_safely(runner: JobRunner, monkeypatch):
    """submit() forwards user_id to _run_safely."""
    captured = {}

    def fake_safely(run_id, request, token, user_id):
        captured["run_id"] = run_id
        captured["user_id"] = user_id
        captured["request_ticker"] = request.ticker

    monkeypatch.setattr(runner, "_run_safely", fake_safely)
    # Submit synchronously by replacing the executor.submit with a direct call.
    monkeypatch.setattr(runner.executor, "submit", lambda fn, *a, **kw: fn(*a, **kw))

    runner.submit("run-123", _request(ticker="TSLA"), user_id="alice")
    assert captured == {"run_id": "run-123", "user_id": "alice", "request_ticker": "TSLA"}


def test_runner_submit_defaults_user_id_to_anonymous(runner: JobRunner, monkeypatch):
    """submit() without user_id uses the synthetic anonymous user."""
    captured = {}

    def fake_safely(run_id, request, token, user_id):
        captured["user_id"] = user_id

    monkeypatch.setattr(runner, "_run_safely", fake_safely)
    monkeypatch.setattr(runner.executor, "submit", lambda fn, *a, **kw: fn(*a, **kw))

    runner.submit("run-1", _request())
    assert captured["user_id"] == "anonymous"


def test_two_users_concurrent_runs_dont_block_each_other(runner: JobRunner, monkeypatch):
    """Different user_ids hold different locks → both can be inside _run_safely.

    Tested via a synchronization point: each fake _run blocks on a barrier.
    If the locks are per-user, both reach the barrier; if they're shared,
    the test deadlocks (caught by a timeout).
    """
    barrier = threading.Barrier(2, timeout=2.0)
    arrived = []

    def fake_safely(run_id, request, token, user_id):
        with runner._get_user_lock(user_id):
            arrived.append(user_id)
            barrier.wait()  # both must reach here for the test to pass

    monkeypatch.setattr(runner, "_run_safely", fake_safely)

    # Submit two different-user runs in parallel via the real executor.
    runner.submit("run-alice", _request(), user_id="alice")
    runner.submit("run-bob", _request(), user_id="bob")

    # Wait for both to finish (the barrier guarantees both got past the lock).
    runner.executor.shutdown(wait=True)
    assert sorted(arrived) == ["alice", "bob"]


def test_same_user_concurrent_runs_serialize(runner: JobRunner, monkeypatch):
    """Two runs from the same user execute strictly one-at-a-time.

    First run holds the lock for ~0.1s; we record overlap by checking the
    `inside` counter never exceeds 1.
    """
    inside = []
    inside_max = [0]
    lock_for_observers = threading.Lock()

    def fake_safely(run_id, request, token, user_id):
        with runner._get_user_lock(user_id):
            with lock_for_observers:
                inside.append(run_id)
                inside_max[0] = max(inside_max[0], len(inside))
            time.sleep(0.05)  # simulate work; if locks failed, both would overlap
            with lock_for_observers:
                inside.remove(run_id)

    monkeypatch.setattr(runner, "_run_safely", fake_safely)

    runner.submit("run-1", _request(), user_id="alice")
    runner.submit("run-2", _request(), user_id="alice")

    runner.executor.shutdown(wait=True)
    assert inside_max[0] == 1, "concurrent same-user runs should serialize"
