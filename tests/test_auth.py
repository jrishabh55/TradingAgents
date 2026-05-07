"""Auth middleware behavior across the three modes (Clerk / legacy / open)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.auth import (
    ANONYMOUS_USER_ID,
    SHARED_BEARER_USER_ID,
    auth_middleware,
    reset_verifier_for_tests,
)


# ---------- helpers ----------


def _build_app() -> FastAPI:
    """Tiny FastAPI app whose only route echoes request.state.user_id.

    Lets us assert exactly what the auth middleware resolved without dragging
    in the full apps/api + sqlite + runner stack.
    """
    app = FastAPI()
    app.middleware("http")(auth_middleware)

    @app.get("/api/whoami2")
    def whoami2(request: Request) -> dict:
        return {"user_id": getattr(request.state, "user_id", None)}

    @app.get("/health")
    def health(request: Request) -> dict:
        return {"user_id": getattr(request.state, "user_id", None)}

    return app


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with no auth env configured, so previous tests don't bleed."""
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    monkeypatch.delenv("WEBAPP_AUTH_TOKEN", raising=False)
    reset_verifier_for_tests()
    yield
    reset_verifier_for_tests()


# ---------- mode: open ----------


def test_open_mode_anonymous_user():
    """No env configured → every request resolves to the synthetic anonymous user."""
    client = TestClient(_build_app())
    r = client.get("/api/whoami2")
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    assert r.json()["user_id"] == ANONYMOUS_USER_ID


def test_open_mode_health_skips_auth():
    """/health is in the bypass list."""
    client = TestClient(_build_app())
    r = client.get("/health")
    assert r.status_code == 200


# ---------- mode: legacy shared bearer ----------


def test_legacy_bearer_accepts_correct_token(monkeypatch):
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json()["user_id"] == SHARED_BEARER_USER_ID


def test_legacy_bearer_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    client = TestClient(_build_app())
    r = client.get("/api/whoami2")
    assert r.status_code == 401


def test_legacy_bearer_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": "Bearer nope"})
    assert r.status_code == 401


def test_legacy_bearer_health_still_open(monkeypatch):
    """Healthcheck must remain reachable even when auth is on (for liveness probes)."""
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    client = TestClient(_build_app())
    r = client.get("/health")
    assert r.status_code == 200


# ---------- mode: Clerk JWT ----------


@pytest.fixture()
def rsa_keypair():
    """Generate a fresh RSA keypair for signing test JWTs."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    return private, public


def _jwk_from_public_key(public_key, kid: str = "test-kid") -> dict:
    """Serialize an RSA public key as a JWK dict (n, e, kty, kid, alg, use)."""
    numbers = public_key.public_numbers()

    def _b64u(value: int) -> str:
        # JWK n/e are base64url-encoded big-endian unsigned integers, no padding.
        import base64

        byte_len = (value.bit_length() + 7) // 8
        raw = value.to_bytes(byte_len, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u(numbers.n),
        "e": _b64u(numbers.e),
    }


def _serve_jwks_file(tmp_path: Path, public_key, kid: str = "test-kid") -> str:
    """Write a JWKs JSON to disk and return a file:// URL.

    PyJWKClient accepts file:// URLs through urllib.
    """
    import json

    jwks = {"keys": [_jwk_from_public_key(public_key, kid=kid)]}
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks))
    return path.as_uri()


def _sign(private_key, claims: dict, kid: str = "test-kid") -> str:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def test_clerk_jwt_accepts_valid_token(monkeypatch, tmp_path, rsa_keypair):
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    token = _sign(
        private,
        {
            "sub": "user_2abcXYZ",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
    )
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == "user_2abcXYZ"


def test_clerk_jwt_accepts_query_param_token(monkeypatch, tmp_path, rsa_keypair):
    """SSE endpoints can't send headers — token in ?token= must work too."""
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    token = _sign(
        private,
        {
            "sub": "user_xyz",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
    )
    client = TestClient(_build_app())
    r = client.get(f"/api/whoami2?token={token}")
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == "user_xyz"


def test_clerk_jwt_rejects_expired_token(monkeypatch, tmp_path, rsa_keypair):
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    token = _sign(
        private,
        {
            "sub": "user_xyz",
            "iat": int(time.time()) - 600,
            "exp": int(time.time()) - 300,  # expired 5min ago
        },
    )
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_clerk_jwt_rejects_token_from_wrong_signer(monkeypatch, tmp_path, rsa_keypair):
    """A token signed with a different keypair than the JWKs publishes is rejected."""
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    # Sign with a DIFFERENT private key.
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(
        other_private,
        {
            "sub": "user_xyz",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
    )
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": f"Bearer {token}"})
    # Either 401 ("invalid token") because signature doesn't match — exact
    # error string varies by PyJWT version.
    assert r.status_code == 401


def test_clerk_jwt_rejects_missing_sub(monkeypatch, tmp_path, rsa_keypair):
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    token = _sign(
        private,
        {"iat": int(time.time()), "exp": int(time.time()) + 300},  # no 'sub'
    )
    client = TestClient(_build_app())
    r = client.get("/api/whoami2", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_clerk_jwt_enforces_issuer_when_configured(monkeypatch, tmp_path, rsa_keypair):
    private, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)
    monkeypatch.setenv("CLERK_ISSUER", "https://expected.clerk.dev")

    # Issuer mismatch.
    bad_iss = _sign(
        private,
        {
            "sub": "user_xyz",
            "iss": "https://attacker.clerk.dev",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
    )
    client = TestClient(_build_app())
    r_bad = client.get("/api/whoami2", headers={"authorization": f"Bearer {bad_iss}"})
    assert r_bad.status_code == 401

    # Issuer match.
    good_iss = _sign(
        private,
        {
            "sub": "user_xyz",
            "iss": "https://expected.clerk.dev",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
    )
    r_ok = client.get("/api/whoami2", headers={"authorization": f"Bearer {good_iss}"})
    assert r_ok.status_code == 200


def test_clerk_jwt_missing_token_rejected(monkeypatch, tmp_path, rsa_keypair):
    """When Clerk is configured, a request with no token at all is 401."""
    _, public = rsa_keypair
    jwks_url = _serve_jwks_file(tmp_path, public)
    monkeypatch.setenv("CLERK_JWKS_URL", jwks_url)

    client = TestClient(_build_app())
    r = client.get("/api/whoami2")
    assert r.status_code == 401
    assert "missing" in r.json()["detail"].lower()
