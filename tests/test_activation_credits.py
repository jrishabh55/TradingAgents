"""Activation + credits gate (Clerk privateMetadata) — apps/api/clerk_users.py.

Two layers:
1. clerk_users unit tests against a fake in-memory Clerk Backend API
   (seeding, debit, broke, deleted user).
2. Middleware behavior: a Clerk-authenticated request is 403'd until the
   user's privateMetadata says ``{"activated": true}``; the gate is inert
   without CLERK_SECRET_KEY and in legacy/open modes.
"""
from __future__ import annotations

import time
import urllib.error

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import clerk_users
from apps.api.auth import auth_middleware, reset_verifier_for_tests
from tests.test_auth import _serve_jwks_file, _sign


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    monkeypatch.delenv("WEBAPP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    reset_verifier_for_tests()
    clerk_users.reset_cache_for_tests()
    yield
    reset_verifier_for_tests()
    clerk_users.reset_cache_for_tests()


@pytest.fixture()
def fake_clerk(monkeypatch):
    """In-memory stand-in for the Clerk Backend API's user endpoints."""
    users: dict[str, dict] = {}

    def _request(method: str, path: str, body=None) -> dict:
        user_id = path.split("/")[2]
        if user_id not in users:
            raise urllib.error.HTTPError(path, 404, "not found", None, None)
        if method == "PATCH":
            users[user_id].setdefault("private_metadata", {}).update(
                body["private_metadata"]
            )
        return users[user_id]

    monkeypatch.setattr(clerk_users, "_request", _request)
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    return users


# ---------- clerk_users unit ----------


def test_gate_defaults_to_not_activated(fake_clerk):
    fake_clerk["user_1"] = {"private_metadata": {}}
    assert clerk_users.get_gate("user_1") == clerk_users.Gate(False, 0)


def test_activation_seeds_default_credits(fake_clerk):
    """Admin only flips `activated` — credits appear on first request."""
    fake_clerk["user_1"] = {"private_metadata": {"activated": True}}
    gate = clerk_users.get_gate("user_1")
    assert gate == clerk_users.Gate(True, clerk_users.DEFAULT_CREDITS)
    # ...and are persisted back to Clerk, without clobbering `activated`.
    assert fake_clerk["user_1"]["private_metadata"] == {
        "activated": True,
        "credits": clerk_users.DEFAULT_CREDITS,
    }


def test_debit_decrements_until_broke(fake_clerk):
    fake_clerk["user_1"] = {"private_metadata": {"activated": True, "credits": 2}}
    assert clerk_users.debit_credit("user_1") == 1
    assert clerk_users.debit_credit("user_1") == 0
    assert clerk_users.debit_credit("user_1") is None
    assert fake_clerk["user_1"]["private_metadata"]["credits"] == 0


def test_debit_refuses_non_activated_user(fake_clerk):
    """Deactivating a user mid-flight also cuts off their credits."""
    fake_clerk["user_1"] = {"private_metadata": {"activated": False, "credits": 5}}
    assert clerk_users.debit_credit("user_1") is None
    assert fake_clerk["user_1"]["private_metadata"]["credits"] == 5


def test_debit_sees_dashboard_topup_immediately(fake_clerk):
    """debit reads fresh from Clerk, bypassing the 60s gate cache."""
    fake_clerk["user_1"] = {"private_metadata": {"activated": True, "credits": 0}}
    clerk_users.get_gate("user_1")  # populate the cache at 0
    fake_clerk["user_1"]["private_metadata"]["credits"] = 3  # dashboard top-up
    assert clerk_users.debit_credit("user_1") == 2


def test_deleted_user_is_not_activated(fake_clerk):
    assert clerk_users.get_gate("user_gone") == clerk_users.Gate(False, 0)


def test_gate_serves_stale_cache_on_clerk_outage(fake_clerk, monkeypatch):
    fake_clerk["user_1"] = {"private_metadata": {"activated": True, "credits": 5}}
    assert clerk_users.get_gate("user_1").activated
    # Expire the cache entry, then break the API.
    with clerk_users._cache_lock:
        gate, _ = clerk_users._cache["user_1"]
        clerk_users._cache["user_1"] = (gate, 0.0)
    monkeypatch.setattr(
        clerk_users, "_fetch_gate", lambda uid: (_ for _ in ()).throw(OSError("down"))
    )
    assert clerk_users.get_gate("user_1") == gate


# ---------- middleware ----------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(auth_middleware)

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict:
        gate = getattr(request.state, "gate", None)
        return {
            "user_id": request.state.user_id,
            "credits": None if gate is None else gate.credits,
        }

    return app


@pytest.fixture()
def clerk_client(monkeypatch, tmp_path):
    """TestClient in Clerk mode + a valid JWT for user_2abc."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("CLERK_JWKS_URL", _serve_jwks_file(tmp_path, private.public_key()))
    token = _sign(
        private,
        {"sub": "user_2abc", "iat": int(time.time()), "exp": int(time.time()) + 300},
    )
    return TestClient(_build_app()), {"authorization": f"Bearer {token}"}


def test_non_activated_user_gets_403(clerk_client, fake_clerk):
    client, headers = clerk_client
    fake_clerk["user_2abc"] = {"private_metadata": {}}
    r = client.get("/api/whoami", headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_activated"


def test_activated_user_passes_with_gate_on_state(clerk_client, fake_clerk):
    client, headers = clerk_client
    fake_clerk["user_2abc"] = {"private_metadata": {"activated": True, "credits": 7}}
    r = client.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"user_id": "user_2abc", "credits": 7}


def test_gate_off_without_secret_key(clerk_client, monkeypatch):
    """JWKS-only deployments keep today's behavior: valid JWT is enough."""
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    client, headers = clerk_client
    r = client.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"user_id": "user_2abc", "credits": None}


def test_clerk_outage_with_cold_cache_is_503_not_lockout(clerk_client, monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(
        clerk_users, "_fetch_gate", lambda uid: (_ for _ in ()).throw(OSError("down"))
    )
    client, headers = clerk_client
    r = client.get("/api/whoami", headers=headers)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "activation_check_failed"


def test_legacy_and_open_modes_skip_gate(monkeypatch):
    """Synthetic users have no Clerk record — the gate must not run."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    # If the gate ran, this would blow up loudly instead of 403/500.
    monkeypatch.setattr(
        clerk_users, "get_gate", lambda uid: (_ for _ in ()).throw(AssertionError)
    )
    client = TestClient(_build_app())
    assert client.get("/api/whoami").status_code == 200  # open mode

    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    r = client.get("/api/whoami", headers={"authorization": "Bearer s3cret"})
    assert r.status_code == 200  # legacy mode
