"""Gemini BYOC key storage + /api/keys/gemini routes — apps/api/user_keys.py.

The security-critical assertions: the key is Fernet ciphertext in SQLite (a DB
copy reveals nothing), it never comes back out of the API (only last4), and
nothing stores it when the encryption secret is unset.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import clerk_users, user_keys
from apps.api.jobs.store import reset_store_for_tests


@pytest.fixture()
def store(tmp_path):
    return reset_store_for_tests(tmp_path / "keys.sqlite")


@pytest.fixture()
def fernet_secret(monkeypatch):
    from cryptography.fernet import Fernet

    secret = Fernet.generate_key().decode()
    monkeypatch.setenv(user_keys.ENCRYPTION_KEY_ENV, secret)
    return secret


@pytest.fixture(autouse=True)
def _no_secret_leaks(monkeypatch):
    monkeypatch.delenv(user_keys.ENCRYPTION_KEY_ENV, raising=False)
    monkeypatch.delenv(user_keys.PROJECT_ENV, raising=False)
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)


# ---------- encryption round-trip ----------


def test_key_round_trips_and_is_ciphertext_at_rest(store, fernet_secret):
    user_keys.save_gemini_key("u1", "AIzaSyFAKE-key-1234")
    assert user_keys.load_gemini_key("u1") == "AIzaSyFAKE-key-1234"
    stored = store.get_user_key("u1", user_keys.GEMINI_PROVIDER)
    assert stored and "AIzaSyFAKE" not in stored


def test_save_refuses_without_encryption_secret(store):
    with pytest.raises(RuntimeError, match=user_keys.ENCRYPTION_KEY_ENV):
        user_keys.save_gemini_key("u1", "AIzaSyFAKE")


def test_rotated_secret_reads_as_absent_not_crash(store, fernet_secret, monkeypatch):
    from cryptography.fernet import Fernet

    user_keys.save_gemini_key("u1", "AIzaSyFAKE")
    monkeypatch.setenv(user_keys.ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())
    assert user_keys.load_gemini_key("u1") is None


def test_delete_removes_the_key(store, fernet_secret):
    user_keys.save_gemini_key("u1", "AIzaSyFAKE")
    assert user_keys.delete_gemini_key("u1") is True
    assert user_keys.load_gemini_key("u1") is None
    assert user_keys.delete_gemini_key("u1") is False


def test_keys_are_per_user(store, fernet_secret):
    user_keys.save_gemini_key("u1", "AIzaSyUSER1")
    assert user_keys.load_gemini_key("u2") is None


# ---------- credential resolution ----------


@pytest.fixture()
def oauth_on(monkeypatch):
    """The OAuth path is shipped OFF (manual keys only) — these tests flip it
    on so the plumbing stays covered for when the product re-enables it."""
    monkeypatch.setattr(user_keys, "OAUTH_ENABLED", True)


def test_oauth_is_disabled_by_default(store, monkeypatch):
    """Even with a token and project available, only manual keys count."""
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    monkeypatch.setenv(user_keys.PROJECT_ENV, "my-gcp-project")
    assert user_keys.gemini_credential_available("u1") is False
    with pytest.raises(RuntimeError, match="API key"):
        user_keys.resolve_gemini_provider_kwargs("u1")
    assert _client().get("/api/keys/gemini").json()["oauth_available"] is False


def test_manual_key_wins_over_oauth(store, fernet_secret, oauth_on, monkeypatch):
    user_keys.save_gemini_key("u1", "AIzaSyFAKE")
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    assert user_keys.resolve_gemini_provider_kwargs("u1") == {"api_key": "AIzaSyFAKE"}


def test_oauth_token_becomes_a_credentials_object(store, oauth_on, monkeypatch):
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    monkeypatch.setenv(user_keys.PROJECT_ENV, "my-gcp-project")
    kwargs = user_keys.resolve_gemini_provider_kwargs("u1")
    creds = kwargs["credentials"]
    assert creds.token == "ya29.tok"
    assert creds.quota_project_id == "my-gcp-project"
    assert "api_key" not in kwargs


def test_oauth_without_project_env_is_unusable(store, oauth_on, monkeypatch):
    """A credentials object forces the Vertex AI backend, which needs the
    project — resolving without it would fail mid-run on an ADC lookup."""
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    with pytest.raises(RuntimeError, match="API key"):
        user_keys.resolve_gemini_provider_kwargs("u1")
    assert user_keys.gemini_credential_available("u1") is False
    ok, message = user_keys.verify_gemini(oauth_token="ya29.tok")
    assert ok is False and user_keys.PROJECT_ENV in message


def test_no_credential_raises_with_the_fix(store, monkeypatch):
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    with pytest.raises(RuntimeError, match="API key"):
        user_keys.resolve_gemini_provider_kwargs("u1")


def test_credential_available_checks_both_sources(store, fernet_secret, oauth_on, monkeypatch):
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    assert user_keys.gemini_credential_available("u1") is False
    user_keys.save_gemini_key("u1", "AIzaSyFAKE")
    assert user_keys.gemini_credential_available("u1") is True
    user_keys.delete_gemini_key("u1")
    monkeypatch.setenv(user_keys.PROJECT_ENV, "my-gcp-project")
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    assert user_keys.gemini_credential_available("u1") is True


# ---------- routes ----------


def _client() -> TestClient:
    from apps.api.routes.keys import router

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = "u1"
        return await call_next(request)

    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_put_verifies_encrypts_and_never_echoes(store, fernet_secret, monkeypatch):
    monkeypatch.setattr(user_keys, "verify_gemini", lambda **kw: (True, "ok"))
    c = _client()
    r = c.put("/api/keys/gemini", json={"api_key": "AIzaSyFAKE-key-1234"})
    assert r.status_code == 200
    body = r.json()
    assert body["manual_key"] is True and body["last4"] == "1234"
    assert "AIzaSyFAKE" not in r.text
    assert user_keys.load_gemini_key("u1") == "AIzaSyFAKE-key-1234"


def test_put_rejects_a_key_the_probe_refuses(store, fernet_secret, monkeypatch):
    monkeypatch.setattr(
        user_keys, "verify_gemini", lambda **kw: (False, "Gemini API rejected the credential (HTTP 400)")
    )
    r = _client().put("/api/keys/gemini", json={"api_key": "AIzaSyBAD-key"})
    assert r.status_code == 400
    assert "rejected" in r.json()["detail"]
    assert user_keys.load_gemini_key("u1") is None


def test_put_is_503_without_encryption_secret(store):
    r = _client().put("/api/keys/gemini", json={"api_key": "AIzaSyFAKE-key"})
    assert r.status_code == 503
    assert user_keys.ENCRYPTION_KEY_ENV in r.json()["detail"]


def test_status_reports_manual_key_without_probing_google(store, fernet_secret, monkeypatch):
    user_keys.save_gemini_key("u1", "AIzaSyFAKE-key-1234")
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    monkeypatch.setattr(
        user_keys, "verify_gemini", lambda **kw: (_ for _ in ()).throw(AssertionError)
    )
    r = _client().get("/api/keys/gemini")
    assert r.status_code == 200
    body = r.json()
    assert body["active_source"] == "manual" and body["last4"] == "1234"


def test_status_reports_oauth_with_probe_verdict(store, oauth_on, monkeypatch):
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: "ya29.tok")
    monkeypatch.setattr(user_keys, "verify_gemini", lambda **kw: (True, "ok"))
    body = _client().get("/api/keys/gemini").json()
    assert body == {
        "manual_key": False,
        "last4": None,
        "oauth_available": True,
        "oauth_ok": True,
        "oauth_error": None,
        "active_source": "oauth",
    }
    # ...and a failing probe surfaces the reason instead of claiming usable.
    monkeypatch.setattr(user_keys, "verify_gemini", lambda **kw: (False, "quota project missing"))
    body = _client().get("/api/keys/gemini").json()
    assert body["active_source"] is None
    assert body["oauth_error"] == "quota project missing"


def test_delete_route(store, fernet_secret, monkeypatch):
    user_keys.save_gemini_key("u1", "AIzaSyFAKE-key")
    monkeypatch.setattr(clerk_users, "get_google_oauth_token", lambda uid: None)
    c = _client()
    assert c.delete("/api/keys/gemini").json() == {"deleted": True}
    assert c.get("/api/keys/gemini").json()["active_source"] is None
