"""Our own ChatGPT OAuth — used when no local CLI login is available.

This exists so the helper does not require Codex CLI to be installed. It appends
to the same credential chain, so which source supplies the token is invisible to
everything downstream.

Two measured facts drive the design (M0/U3):

**The refresh grant ROTATES the refresh token.** That makes two things mandatory
rather than nice-to-have:

* *Write before use.* The new token pair is persisted BEFORE the access token is
  handed out. Use-then-write means a crash in between leaves the old refresh
  token already invalidated upstream and the new one never saved — a stranded
  session with no recovery but a fresh login.
* *Single-flight.* Two concurrent refreshes are a correctness bug, not a
  performance one: the first rotates the token, so the second presents a
  credential upstream has already invalidated and the session dies. One lock,
  and losers re-read the result rather than issuing their own refresh.

**There is an ``earliest_refresh_at`` floor**, so refreshing eagerly is rejected
upstream. We only refresh inside the expiry margin.

Storage is the atomic 0600 file written by :mod:`apps.helper.paths`. No OS
keychain: adding a dependency for a single 0600 file would not earn its keep.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from apps.helper import paths
from apps.helper.credentials import Credential, CredentialError
from apps.helper.credentials.cli_file import EXPIRY_MARGIN_S, _jwt_claims

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"

#: OpenAI's public Codex client. The Codex backend only accepts tokens minted
#: for this client, and it cannot be registered for independently.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

SCOPES = "openid profile email offline_access"

#: Codex CLI's callback port. Whether any other port is accepted is UNRESOLVED
#: (U1) — a Cloudflare interstitial sits in front of the authorize endpoint, so
#: it could not be probed headlessly. Rather than guess, the port is configurable
#: and the collision is detected and explained at runtime.
DEFAULT_CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"

_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(*, port: int = DEFAULT_CALLBACK_PORT, challenge: str, state: str) -> str:
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(port),
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


def redirect_uri(port: int = DEFAULT_CALLBACK_PORT) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def callback_port_available(port: int = DEFAULT_CALLBACK_PORT) -> bool:
    """Whether we can bind the callback port.

    A live ``codex login`` holds 1455, and U1 leaves open whether any other port
    is accepted — so a collision is reported plainly instead of failing deep in
    the flow with an opaque error.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


@dataclass
class StoredTokens:
    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    #: Unix seconds; from the access token's JWT exp when available.
    expires_at: Optional[int] = None
    #: Upstream floor before another refresh is permitted.
    earliest_refresh_at: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "StoredTokens":
        data = json.loads(raw)
        # A truncated or foreign-schema file must fail here, loudly. Loading
        # access_token=None would flow all the way to the wire as a literal
        # "Authorization: Bearer None" and surface as a baffling upstream 401
        # instead of "credential file is corrupt, log in again".
        if not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise ValueError("credential file has no access_token")
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


class TokenStore:
    """Atomic 0600 storage for our own tokens. Never touches a CLI's file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (paths.state_dir() / "chatgpt_oauth.json")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[StoredTokens]:
        raw = paths.read_secret(self._path)
        if not raw:
            return None
        try:
            return StoredTokens.from_json(raw)
        except (ValueError, TypeError):
            return None

    def save(self, tokens: StoredTokens) -> None:
        paths.write_secret(self._path, tokens.to_json())

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def tokens_from_response(payload: dict[str, Any], previous: Optional[StoredTokens] = None) -> StoredTokens:
    """Build StoredTokens from a token-endpoint response.

    ``refresh_token`` falls back to the previous one only when the response
    omits it — the measured behaviour is that it IS returned and rotated.
    """
    access = payload.get("access_token")
    if not access:
        raise CredentialError("token response contained no access_token")
    exp = _jwt_claims(access).get("exp")
    return StoredTokens(
        access_token=access,
        refresh_token=payload.get("refresh_token") or (previous.refresh_token if previous else ""),
        id_token=payload.get("id_token") or (previous.id_token if previous else None),
        expires_at=int(exp) if isinstance(exp, (int, float)) else None,
        earliest_refresh_at=payload.get("earliest_refresh_at"),
    )


# --------------------------------------------------------------------------
# credential source
# --------------------------------------------------------------------------


class OwnOAuthSource:
    """Serves tokens this helper obtained itself, refreshing when needed."""

    name = "helper-oauth"

    def __init__(self, store: Optional[TokenStore] = None, *, http: Any = None) -> None:
        self._store = store or TokenStore()
        self._http = http
        # Guards the rotate-and-persist critical section. Mandatory: two
        # concurrent refreshes would rotate twice and invalidate each other.
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        return self._store.load() is not None

    async def get(self) -> Credential:
        tokens = self._store.load()
        if tokens is None:
            raise CredentialError(
                "this helper has no ChatGPT login of its own",
                remedy="Run `python -m apps.helper login` to sign in.",
            )

        if self._needs_refresh(tokens):
            async with self._lock:
                # The asyncio lock covers this process; the file lock covers
                # `serve` and `connect` running as SEPARATE processes against
                # the same token file — a cross-process double refresh rotates
                # twice and strands the session just as surely.
                fh = await asyncio.to_thread(_acquire_refresh_lock, self._store.path)
                try:
                    # Re-read inside the lock: another caller may have refreshed
                    # while we waited, in which case reusing their result is the
                    # only safe move — our copy of the refresh token is now dead.
                    tokens = self._store.load() or tokens
                    if self._needs_refresh(tokens):
                        tokens = await self._refresh(tokens)
                finally:
                    _release_refresh_lock(fh)

        return self._to_credential(tokens)

    # ---- internals ----

    @staticmethod
    def _needs_refresh(tokens: StoredTokens) -> bool:
        if tokens.expires_at is None:
            return False
        return tokens.expires_at - time.time() < EXPIRY_MARGIN_S

    async def _refresh(self, tokens: StoredTokens) -> StoredTokens:
        if not tokens.refresh_token:
            raise CredentialError(
                "stored credential has no refresh token",
                remedy="Run `python -m apps.helper login` to sign in again.",
            )
        floor = tokens.earliest_refresh_at
        if isinstance(floor, (int, float)) and time.time() < floor:
            raise CredentialError(
                "upstream will not permit a refresh yet",
                remedy=f"Retry after {int(floor)} (unix seconds).",
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": CLIENT_ID,
        }
        data = await self._post_token(payload)
        new_tokens = tokens_from_response(data, previous=tokens)

        # WRITE BEFORE USE. Upstream has already invalidated the old refresh
        # token by this point; if persisting fails we must surface that rather
        # than hand out an access token whose refresh token exists only in RAM.
        try:
            self._store.save(new_tokens)
        except OSError as exc:
            raise CredentialError(
                f"refreshed the credential but could not persist it: {exc}. "
                "The previous refresh token is already invalid upstream.",
                remedy="Fix the storage path, then run `python -m apps.helper login`.",
            ) from exc
        return new_tokens

    async def _post_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        client = self._http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        owns = self._http is None
        try:
            res = await client.post(
                TOKEN_URL, json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if res.status_code >= 400:
                raise CredentialError(
                    f"token endpoint returned {res.status_code}: {res.text[:200]}",
                    remedy="Run `python -m apps.helper login` to sign in again.",
                )
            return res.json()
        finally:
            if owns:
                await client.aclose()

    @staticmethod
    def _to_credential(tokens: StoredTokens) -> Credential:
        meta = (_jwt_claims(tokens.id_token).get(_OPENAI_AUTH_CLAIM) or {}) if tokens.id_token else {}
        account = meta.get("chatgpt_account_id")
        headers = {"chatgpt-account-id": str(account)} if account else {}
        return Credential(
            token=tokens.access_token,
            headers=headers,
            account_label=str(account) if account else "helper-oauth",
            plan=meta.get("chatgpt_plan_type"),
            expires_at=tokens.expires_at,
        )


def _acquire_refresh_lock(token_path: Path):
    """Blocking exclusive lock on a sibling of the token file.

    Called via ``asyncio.to_thread`` so the wait doesn't stall the event loop.
    Returns the open handle. POSIX uses ``flock``; Windows uses ``msvcrt``
    (which retries for ~10s and then raises — better a loud failure than a
    silent double refresh that strands the session).
    """
    lock_path = token_path.parent / (token_path.name + ".lock")
    fh = open(lock_path, "a")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except ImportError:
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    return fh


def _release_refresh_lock(fh: Any) -> None:
    if fh is None:
        return
    try:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


async def exchange_code(
    code: str, verifier: str, *, port: int = DEFAULT_CALLBACK_PORT, http: Any = None
) -> StoredTokens:
    """Swap an authorization code for tokens."""
    source = OwnOAuthSource(http=http)
    data = await source._post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri(port),
            "code_verifier": verifier,
        }
    )
    return tokens_from_response(data)
