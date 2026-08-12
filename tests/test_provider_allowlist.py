"""POST /api/runs provider gating: allowlist, Gemini credential 409, and
OpenAI-only credit debits. UI dropdown trimming alone would not stop a direct
API call from running e.g. "anthropic" against server-side env keys — the
allowlist is the server-side enforcement.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import clerk_users, user_keys
from apps.api.jobs.store import reset_store_for_tests
from apps.api.routes import runs as runs_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Runs router with a fake Clerk user, a tmp store, and a mock runner."""
    reset_store_for_tests(tmp_path / "runs.sqlite")
    monkeypatch.setattr(runs_module, "get_runner", lambda: MagicMock())
    # Credits gate off unless a test enables it explicitly.
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = "user_2abc"
        return await call_next(request)

    app.include_router(runs_module.router, prefix="/api")
    return TestClient(app)


def _body(**over) -> dict:
    body = dict(
        ticker="SPY",
        analysis_date="2026-08-01",
        analysts=["market"],
        research_depth=1,
        llm_provider="openai",
        shallow_thinker="gpt-5.4-mini",
        deep_thinker="gpt-5.4",
    )
    body.update(over)
    return body


# ---------- allowlist ----------


@pytest.mark.parametrize("provider", ["anthropic", "xai", "deepseek", "ollama", "azure", ""])
def test_non_product_providers_are_rejected(client, provider):
    r = client.post("/api/runs", json=_body(llm_provider=provider))
    assert r.status_code == 400
    assert "unsupported llm_provider" in r.json()["detail"]


@pytest.mark.parametrize("provider", ["openai", "OpenAI", "google", "chatgpt_helper"])
def test_product_providers_pass_the_allowlist(client, provider):
    """Proven by tripping the NEXT check (bad date), not the allowlist."""
    r = client.post("/api/runs", json=_body(llm_provider=provider, analysis_date="bad"))
    assert r.status_code == 400
    assert "analysis_date" in r.json()["detail"]


# ---------- Gemini submit-time credential check ----------


def test_google_without_credential_is_409_with_the_fix(client, monkeypatch):
    monkeypatch.setattr(user_keys, "gemini_credential_available", lambda uid: False)
    r = client.post("/api/runs?skip_preflight=true", json=_body(llm_provider="google"))
    assert r.status_code == 409
    assert "API key" in r.json()["detail"]


def test_google_with_credential_is_accepted(client, monkeypatch):
    monkeypatch.setattr(user_keys, "gemini_credential_available", lambda uid: True)
    r = client.post("/api/runs?skip_preflight=true", json=_body(llm_provider="google"))
    assert r.status_code == 201


# ---------- credits: only OpenAI (server-key) runs debit ----------


def test_openai_run_debits_a_credit(client, monkeypatch):
    monkeypatch.setattr(clerk_users, "enabled", lambda: True)
    debit = MagicMock(return_value=5)
    monkeypatch.setattr(clerk_users, "debit_credit", debit)
    r = client.post("/api/runs?skip_preflight=true", json=_body())
    assert r.status_code == 201
    debit.assert_called_once_with("user_2abc")


def test_gemini_byoc_run_is_free(client, monkeypatch):
    """The user's own credential pays Google — no credit debit."""
    monkeypatch.setattr(clerk_users, "enabled", lambda: True)
    monkeypatch.setattr(user_keys, "gemini_credential_available", lambda uid: True)
    monkeypatch.setattr(
        clerk_users, "debit_credit", lambda uid: (_ for _ in ()).throw(AssertionError)
    )
    r = client.post("/api/runs?skip_preflight=true", json=_body(llm_provider="google"))
    assert r.status_code == 201


def test_broke_user_can_still_run_gemini_but_not_openai(client, monkeypatch):
    monkeypatch.setattr(clerk_users, "enabled", lambda: True)
    monkeypatch.setattr(clerk_users, "debit_credit", lambda uid: None)  # out of credits
    monkeypatch.setattr(user_keys, "gemini_credential_available", lambda uid: True)

    r = client.post("/api/runs?skip_preflight=true", json=_body())
    assert r.status_code == 402

    r = client.post("/api/runs?skip_preflight=true", json=_body(llm_provider="google"))
    assert r.status_code == 201
