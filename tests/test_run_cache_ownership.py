"""POST /api/runs cache must never return a run id the caller can't GET.

Regression for a prod bug: the cache lookup was shared across users, so user B
POSTing a request user A had completed got back A's run id with HTTP 200 — and
the immediate GET /api/runs/{id} 404'd on the ownership check ("run not found"
right after creating a run). The cache is now scoped per-user.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.jobs.store import reset_store_for_tests
from apps.api.routes import runs as runs_module
from apps.api.schemas import RunRequest, request_hash

BODY = dict(
    ticker="SPY",
    analysis_date="2026-08-01",
    analysts=["market"],
    research_depth=1,
    llm_provider="openai",
    shallow_thinker="gpt-5.4-mini",
    deep_thinker="gpt-5.4",
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = reset_store_for_tests(tmp_path / "runs.sqlite")
    monkeypatch.setattr(runs_module, "get_runner", lambda: MagicMock())
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)

    current_user = {"id": "user_a"}
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = current_user["id"]
        return await call_next(request)

    app.include_router(runs_module.router, prefix="/api")
    return TestClient(app), store, current_user


def _completed_run_for(store, user_id: str) -> str:
    req = RunRequest(**BODY)
    run_id = store.create_run(
        ticker=req.ticker,
        analysis_date=req.analysis_date,
        config=req.model_dump(),
        request_hash=request_hash(req),
        user_id=user_id,
    )
    store.mark_running(run_id)
    store.mark_completed(
        run_id,
        decision_text="HOLD",
        rating="Hold",
        final_state={"final_trade_decision": "HOLD"},
    )
    return run_id


def test_cache_hit_is_scoped_to_the_caller(env):
    client, store, current_user = env
    a_run = _completed_run_for(store, "user_a")

    # User B: no cache hit on A's run — fresh 201, and the id is fetchable.
    current_user["id"] = "user_b"
    r = client.post("/api/runs?skip_preflight=true", json=BODY)
    assert r.status_code == 201
    assert r.json()["id"] != a_run
    assert client.get(f"/api/runs/{r.json()['id']}").status_code == 200

    # User A still gets their own cached run back.
    current_user["id"] = "user_a"
    r = client.post("/api/runs?skip_preflight=true", json=BODY)
    assert r.status_code == 200
    assert r.json()["id"] == a_run
    assert r.json()["cached"] is True
    assert client.get(f"/api/runs/{a_run}").status_code == 200
