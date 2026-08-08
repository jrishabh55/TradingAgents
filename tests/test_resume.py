"""Checkpoint resume (M4).

The Codex review found the original design wrong in three independent ways, so
each has a test pinning the corrected behaviour:

* the checkpoint namespace must be per-RUN, not ticker+date (which collides
  across users, since the API permits the same ticker concurrently)
* resume must stream with ``input=None``; resending ``init_state`` restarts the
  graph, which is what resume exists to avoid
* the translator must be seeded from storage, or a fresh sequence counter
  collides with the events already persisted for the run
"""
from datetime import datetime
from pathlib import Path

import pytest

from apps.api.integrations.graph_factory import checkpoint_namespace
from apps.api.jobs.runner import _prior_state
from apps.api.jobs.store import JobStore
from apps.api.jobs.translator import ChunkTranslator
from apps.api.schemas import RunDetail


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "runs.sqlite")


def _run(store, *, user="u1", ticker="AAPL"):
    return store.create_run(
        ticker=ticker, analysis_date="2026-08-01",
        config={"ticker": ticker}, request_hash="h", user_id=user,
    )


# ---------- namespace scoping ----------


def test_namespace_is_unique_per_run():
    assert checkpoint_namespace("run-a", "u1") != checkpoint_namespace("run-b", "u1")


def test_namespace_is_unique_per_user_for_the_same_run_id():
    """Two users analysing the same ticker must not share a checkpoint."""
    assert checkpoint_namespace("run-a", "u1") != checkpoint_namespace("run-a", "u2")


def test_namespace_sanitizes_hostile_user_ids():
    ns = checkpoint_namespace("run-a", "../../etc/passwd")
    assert "/" not in ns and ".." not in ns


def test_namespace_handles_absent_user():
    assert checkpoint_namespace("r", None).startswith("anonymous:")


def test_a_real_namespace_yields_a_valid_checkpoint_db_key():
    """Regression: the raw namespace (user:uuid, 40+ chars with a ':') fails
    upstream's safe_ticker_component unconditionally — every checkpoint-enabled
    run crashed. The DB filename must be the hashed key, and hashing must not
    collapse distinct namespaces."""
    import uuid

    from apps.api.integrations.graph_factory import checkpoint_db_key
    from tradingagents.dataflows.utils import safe_ticker_component

    ns = checkpoint_namespace(str(uuid.uuid4()), "user_2abcXYZ")
    with pytest.raises(ValueError):
        safe_ticker_component(ns)
    key = checkpoint_db_key(ns)
    assert safe_ticker_component(key) == key
    assert checkpoint_db_key(checkpoint_namespace("other-run", "user_2abcXYZ")) != key


# ---------- sweep ----------


def test_sweep_marks_queued_and_running_as_interrupted(store):
    """A queued row's future died with the previous executor just as surely as a
    running one, and leaving it queued means it is never picked up."""
    queued = _run(store)
    running = _run(store, ticker="TSLA")
    store.mark_running(running)

    assert store.sweep_orphaned_runs() == 2
    assert store.get_run(queued).status == "interrupted"
    assert store.get_run(running).status == "interrupted"


def test_sweep_leaves_terminal_runs_alone(store):
    done = _run(store)
    store.mark_running(done)
    store.mark_completed(done, decision_text="d", rating="Hold", final_state={})
    failed = _run(store, ticker="MSFT")
    store.mark_failed(failed, "boom")

    assert store.sweep_orphaned_runs() == 0
    assert store.get_run(done).status == "completed"
    assert store.get_run(failed).status == "failed"


def test_sweep_is_idempotent(store):
    r = _run(store)
    store.mark_running(r)
    assert store.sweep_orphaned_runs() == 1
    assert store.sweep_orphaned_runs() == 0


def test_sweep_explains_itself(store):
    r = _run(store)
    store.mark_running(r)
    store.sweep_orphaned_runs()
    assert "restarted" in (store.get_run(r).error or "")


def test_sweep_preserves_an_existing_error_message(store):
    r = _run(store)
    store.mark_running(r)
    store.mark_interrupted(r, "vendor exploded")
    # Re-sweeping must not overwrite the specific reason with the generic one.
    store.sweep_orphaned_runs()
    assert store.get_run(r).error == "vendor exploded"


# ---------- atomic claim ----------


def test_claim_succeeds_once_and_only_once(store):
    r = _run(store)
    store.mark_running(r)
    store.sweep_orphaned_runs()
    assert store.claim_for_resume(r, user_id="u1") is True
    assert store.claim_for_resume(r, user_id="u1") is False


def test_claim_moves_the_run_to_running_and_clears_the_error(store):
    r = _run(store)
    store.mark_running(r)
    store.sweep_orphaned_runs()
    store.claim_for_resume(r, user_id="u1")
    d = store.get_run(r)
    assert d.status == "running" and d.error is None


def test_claim_refuses_another_users_run(store):
    r = _run(store, user="owner")
    store.mark_running(r)
    store.sweep_orphaned_runs()
    assert store.claim_for_resume(r, user_id="someone-else") is False
    assert store.get_run(r).status == "interrupted"


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_claim_refuses_non_interrupted_runs(store, terminal):
    r = _run(store)
    store.mark_running(r)
    {"completed": lambda: store.mark_completed(r, decision_text="d", rating="Hold", final_state={}),
     "failed": lambda: store.mark_failed(r, "e"),
     "cancelled": lambda: store.mark_cancelled(r)}[terminal]()
    assert store.claim_for_resume(r, user_id="u1") is False


def test_resumes_are_capped(store):
    """Each resume re-bills the calls after the last checkpoint, so a run that
    dies the same way every time must stop being resumable."""
    from apps.api.jobs.store import MAX_RESUMES

    r = _run(store)
    for _ in range(MAX_RESUMES):
        store.sweep_orphaned_runs()
        assert store.claim_for_resume(r, user_id="u1") is True
    store.sweep_orphaned_runs()
    assert store.claim_for_resume(r, user_id="u1") is False


def test_concurrent_claims_yield_exactly_one_winner(store):
    """The guard is in the WHERE clause, not a read-then-write."""
    import threading

    r = _run(store)
    store.mark_running(r)
    store.sweep_orphaned_runs()
    wins = []
    barrier = threading.Barrier(6)

    def attempt():
        barrier.wait()
        wins.append(store.claim_for_resume(r, user_id="u1"))

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


# ---------- checkpoint context ----------


def test_checkpoint_context_round_trips(store):
    r = _run(store)
    store.set_checkpoint_context(
        r, checkpoint_ns="u1:abc", effective_config={"temperature": 0.0, "x": 1}
    )
    ctx = store.get_checkpoint_context(r)
    assert ctx["checkpoint_ns"] == "u1:abc"
    assert ctx["effective_config"]["temperature"] == 0.0


def test_no_checkpoint_context_reads_as_none(store):
    assert store.get_checkpoint_context(_run(store)) is None


def test_effective_config_is_recorded_because_config_json_is_only_the_request(store):
    """A restart re-applies an env-derived DEFAULT_CONFIG, so without this a
    resume could silently run under a different temperature or retry policy."""
    r = _run(store)
    store.set_checkpoint_context(
        r, checkpoint_ns="ns", effective_config={"llm_max_retries": 6}
    )
    assert store.get_checkpoint_context(r)["effective_config"]["llm_max_retries"] == 6
    # config_json still holds the request, unchanged.
    assert store.get_run(r).config == {"ticker": "AAPL"}


# ---------- translator seeding ----------


def test_translator_starts_at_the_stored_sequence(store):
    """A fresh counter would collide with events already persisted for the run,
    violating UNIQUE(run_id, seq)."""
    t = ChunkTranslator("r", selected_analysts=["market"], start_seq=42)
    env = t.emit_run_started(ticker="A", analysis_date="2026-08-01", config={})
    assert env.seq == 43


def test_translator_defaults_to_zero_for_a_fresh_run():
    t = ChunkTranslator("r", selected_analysts=["market"])
    assert t.emit_run_started(ticker="A", analysis_date="2026-08-01", config={}).seq == 1


def test_translator_seeded_state_is_not_re_emitted():
    """Sections the interrupted attempt produced are known, not new."""
    t = ChunkTranslator(
        "r", selected_analysts=["market", "news"],
        replay_state={"market_report": "already done"},
    )
    assert t.final_state["market_report"] == "already done"
    events = list(t.handle_chunk({"market_report": "already done"}))
    # market is already completed, so no analyst.completed fires again for it.
    assert not [e for e in events
                if e.type == "analyst.completed" and e.data.get("analyst") == "market"]


def test_translator_seeded_debate_streams_only_the_new_tail():
    """values-mode chunks carry the FULL debate history; after a resume only
    the text produced since the interruption may be streamed, and the team
    must not be re-announced as started."""
    prior = {"risk_debate_state": {"aggressive_history": "OLD"}}
    t = ChunkTranslator("r", selected_analysts=["market"], replay_state=prior)
    events = list(t.handle_chunk({"risk_debate_state": {"aggressive_history": "OLDNEW"}}))
    updates = [e for e in events if e.type == "debate.update"]
    assert [u.data["delta"] for u in updates] == ["NEW"]
    assert not [e for e in events if e.type == "team.started"]


def test_translator_still_reports_analysts_that_had_not_finished():
    t = ChunkTranslator(
        "r", selected_analysts=["market", "news"],
        replay_state={"market_report": "done"},
    )
    events = list(t.handle_chunk({"news_report": "fresh"}))
    assert [e.data["analyst"] for e in events if e.type == "analyst.completed"] == ["news"]


# ---------- prior-state extraction ----------


def test_prior_state_collects_only_populated_sections():
    d = RunDetail(
        id="r", ticker="A", analysis_date="2026-08-01", status="interrupted",
        created_at=datetime(2026, 8, 1), config={},
        market_report="m", news_report=None,
    )
    state = _prior_state(d)
    assert state == {"market_report": "m"}


def test_prior_state_of_none_is_none():
    assert _prior_state(None) is None
