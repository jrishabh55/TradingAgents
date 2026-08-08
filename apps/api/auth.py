"""Authentication for the API.

Three modes, in order of preference:

1. **Clerk JWT** — when ``CLERK_JWKS_URL`` is set. Every ``/api/*`` request
   must carry an ``Authorization: Bearer <jwt>`` header. The JWT is verified
   against Clerk's JWKs and the ``sub`` claim (a Clerk user id like
   ``user_2abcXYZ``) becomes the request's user identity.

2. **Legacy shared bearer token** — when only ``WEBAPP_AUTH_TOKEN`` is set
   (Clerk not configured). One token is shared by all callers; every request
   gets a synthetic ``shared-bearer`` user id. Useful for personal/internal
   deployments before you provision Clerk.

3. **Open** — neither is set. Every request is treated as the synthetic
   ``anonymous`` user. Same behavior as the original webapp pre-auth.

In all three modes, ``request.state.user_id`` is populated by the middleware
and the ``current_user_id`` dependency exposes it to routes.

Provisioning Clerk:
- Sign up at https://clerk.com, create an application
- Copy the JWKs URL: <CLERK_FRONTEND_API>/.well-known/jwks.json
- Set ``CLERK_JWKS_URL`` (and optionally ``CLERK_ISSUER`` to pin iss)
- The frontend uses ``@clerk/clerk-react`` and attaches the session JWT
  via ``useAuth().getToken()``
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient


logger = logging.getLogger(__name__)


# Synthetic user ids used when no real auth is configured. Stable strings so
# they index cleanly in the runs.user_id column.
ANONYMOUS_USER_ID = "anonymous"
SHARED_BEARER_USER_ID = "shared-bearer"


# Paths that bypass auth entirely. The SPA needs to load before it can attach
# a token, and oncall scripts hit /health without a token.
# /api/helper/download serves the packaged helper app — a public artifact a
# not-yet-authenticated user needs, and a bare <a href> carries no bearer.
# /api/helper/version is its version metadata: running helpers poll it for
# update checks and hold a relay pairing token, not a Clerk JWT.
_BYPASS_PATHS = ("/", "/health", "/api/helper/download", "/api/helper/version")
# /internal/relay is the pipeline worker calling back into this server with a
# per-run internal token — it has no Clerk JWT. The shim authenticates that
# token itself (routes/relay.py), which is stronger than the shared bearer:
# the token is minted per run and revoked when the run ends.
_BYPASS_PREFIXES = ("/static", "/internal/relay/")


def _bypass(path: str) -> bool:
    return path in _BYPASS_PATHS or any(path.startswith(p) for p in _BYPASS_PREFIXES)


class ClerkVerifier:
    """Verifies Clerk-issued JWTs against the configured JWKs.

    Wraps ``jwt.PyJWKClient`` which handles the JWK fetching and caching with
    a default 5-minute TTL. ``CLERK_ISSUER`` is checked when set so a token
    issued by a different Clerk app can't be replayed against this one.
    """

    def __init__(self, jwks_url: str, issuer: Optional[str] = None) -> None:
        self.jwks_url = jwks_url
        self.issuer = issuer
        # PyJWKClient performs HTTP fetches on first key miss. The lifetime
        # is the lifetime of the FastAPI app — keys are cached in-process.
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        self._lock = threading.Lock()

    def verify(self, token: str) -> str:
        """Verify ``token`` and return the Clerk user id (the ``sub`` claim).

        Raises ``HTTPException(401)`` on any verification failure.
        """
        try:
            with self._lock:
                signing_key = self._jwks.get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                # Clerk doesn't always set aud — leave validation off there.
                options={"verify_aud": False, "require": ["sub", "exp"]},
                issuer=self.issuer,  # None → skip issuer check
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="token expired")
        except jwt.InvalidTokenError as exc:
            logger.info("rejected invalid JWT: %s", exc)
            raise HTTPException(status_code=401, detail="invalid token")

        sub = decoded.get("sub")
        if not isinstance(sub, str) or not sub:
            raise HTTPException(status_code=401, detail="token missing sub")
        return sub


_verifier_singleton: Optional[ClerkVerifier] = None
_verifier_lock = threading.Lock()


def get_verifier() -> Optional[ClerkVerifier]:
    """Return the process-wide ClerkVerifier, or None when Clerk isn't configured."""
    global _verifier_singleton
    if _verifier_singleton is not None:
        return _verifier_singleton
    jwks_url = os.environ.get("CLERK_JWKS_URL", "").strip()
    if not jwks_url:
        return None
    with _verifier_lock:
        if _verifier_singleton is None:
            issuer = os.environ.get("CLERK_ISSUER", "").strip() or None
            _verifier_singleton = ClerkVerifier(jwks_url=jwks_url, issuer=issuer)
    return _verifier_singleton


def reset_verifier_for_tests() -> None:
    """Test helper: drop the cached verifier so env-var changes take effect."""
    global _verifier_singleton
    with _verifier_lock:
        _verifier_singleton = None


def _extract_bearer(request: Request) -> Optional[str]:
    """Return the bearer token from the request, or None.

    Checks:
    1. ``Authorization: Bearer <token>`` header (standard for fetch/XHR)
    2. ``?token=<token>`` query param (needed for SSE: native EventSource
       cannot send custom headers, so the frontend appends the JWT here)
    """
    sent = request.headers.get("authorization", "")
    if sent.startswith("Bearer "):
        token = sent[len("Bearer "):].strip()
        if token:
            return token
    qp_token = request.query_params.get("token")
    if qp_token:
        return qp_token.strip() or None
    return None


def _unauth(detail: str) -> JSONResponse:
    """401 response shaped to match FastAPI's HTTPException JSON body.

    Middleware can't raise ``HTTPException`` — Starlette's middleware
    pipeline lets exceptions propagate unwrapped. Returning a JSONResponse
    keeps the error path consistent with the rest of the API.
    """
    return JSONResponse({"detail": detail}, status_code=401)


async def auth_middleware(request: Request, call_next):
    """Resolve ``request.state.user_id`` based on the configured auth mode.

    Order of preference:
      1. Clerk JWT (if CLERK_JWKS_URL set)
      2. Legacy shared bearer (if WEBAPP_AUTH_TOKEN set)
      3. Open (anonymous)

    On verification failure returns HTTP 401. On success the downstream
    handler can read ``request.state.user_id``.
    """
    if _bypass(request.url.path):
        # Static + healthcheck: no user concept needed, but populate a safe
        # default for any code that reads request.state.user_id unconditionally.
        request.state.user_id = ANONYMOUS_USER_ID
        return await call_next(request)

    verifier = get_verifier()
    if verifier is not None:
        token = _extract_bearer(request)
        if token is None:
            return _unauth("missing bearer token")
        try:
            request.state.user_id = verifier.verify(token)
        except HTTPException as exc:
            return _unauth(str(exc.detail))
        return await call_next(request)

    legacy_token = os.environ.get("WEBAPP_AUTH_TOKEN", "").strip()
    if legacy_token:
        token = _extract_bearer(request)
        if token != legacy_token:
            return _unauth("unauthorized")
        request.state.user_id = SHARED_BEARER_USER_ID
        return await call_next(request)

    request.state.user_id = ANONYMOUS_USER_ID
    return await call_next(request)


def current_user_id(request: Request) -> str:
    """FastAPI dependency that returns the resolved user id.

    Always returns a non-empty string — the middleware populates
    ``request.state.user_id`` for every request that gets this far. Routes
    use this to scope queries by user.
    """
    user_id = getattr(request.state, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        # Defensive: should never happen because the middleware always
        # populates this. If it does, fail closed.
        raise HTTPException(status_code=401, detail="no user id on request")
    return user_id
