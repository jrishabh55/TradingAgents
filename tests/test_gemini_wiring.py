"""Gemini BYOC graph construction — apps/api/integrations/graph_factory.py.

Mirrors test_helper_wiring.py's security assertions for the Gemini paths: the
user credential reaches provider kwargs (in memory) but never the serialized
config, and the server can never silently pay with its own GOOGLE_API_KEY env.
Also covers the backend_url hardening for the plain OpenAI path.
"""
from __future__ import annotations

import pytest

from apps.api import clerk_users, user_keys
from apps.api.integrations.graph_factory import _build_config, build_graph_for_request
from apps.api.integrations.helper_backend import HelperBackedGraph
from apps.api.jobs.store import reset_store_for_tests
from apps.api.schemas import RunRequest


@pytest.fixture()
def store(tmp_path):
    return reset_store_for_tests(tmp_path / "wiring.sqlite")


@pytest.fixture()
def fernet_secret(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(user_keys.ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())


def _request(**over) -> RunRequest:
    body = dict(
        ticker="SPY",
        analysis_date="2026-08-01",
        analysts=["market"],
        research_depth=1,
        llm_provider="google",
        shallow_thinker="gemini-3.5-flash",
        deep_thinker="gemini-3.1-pro-preview",
        backend_url=None,
    )
    body.update(over)
    return RunRequest(**body)


def test_manual_key_reaches_provider_kwargs_but_not_config(store, fernet_secret, monkeypatch):
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    user_keys.save_gemini_key("u1", "AIzaSyUSER-key-1234")
    graph, _, _, _ = build_graph_for_request(_request(), user_id="u1")
    assert isinstance(graph, HelperBackedGraph)
    assert graph._get_provider_kwargs()["api_key"] == "AIzaSyUSER-key-1234"
    assert "AIzaSyUSER" not in str(graph.config)


def test_oauth_token_reaches_provider_kwargs_as_credentials(store, monkeypatch):
    """OAuth ships disabled — flipped on here so the wiring stays covered."""
    monkeypatch.setattr(user_keys, "OAUTH_ENABLED", True)
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.oauth-tok")
    monkeypatch.setenv(user_keys.PROJECT_ENV, "my-gcp-project")
    graph, _, _, _ = build_graph_for_request(_request(), user_id="u1")
    creds = graph._get_provider_kwargs()["credentials"]
    assert creds.token == "ya29.oauth-tok"
    assert creds.quota_project_id == "my-gcp-project"
    assert "ya29" not in str(graph.config)


def test_no_user_credential_refuses_to_build_despite_server_env_key(store, monkeypatch):
    """conftest seeds GOOGLE_API_KEY=placeholder in env — it must NOT be used."""
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    with pytest.raises(RuntimeError, match="API key"):
        build_graph_for_request(_request(), user_id="u1")


def test_client_backend_url_is_ignored_for_every_provider(store, fernet_secret, monkeypatch):
    """request.backend_url is client input; honouring it for openai would send
    the server's OPENAI_API_KEY to an arbitrary caller-chosen host."""
    cfg = _build_config(_request(llm_provider="openai", backend_url="https://attacker.example/v1"))
    assert cfg["backend_url"] == "https://api.openai.com/v1"

    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    user_keys.save_gemini_key("u1", "AIzaSyUSER-key")
    graph, _, _, _ = build_graph_for_request(
        _request(backend_url="https://attacker.example/v1"), user_id="u1"
    )
    assert graph.config["backend_url"] is None
