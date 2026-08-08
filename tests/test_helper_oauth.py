"""Own-OAuth source (M3).

Refresh-token rotation was measured in M0/U3, and it turns two things from
best-practice into correctness requirements: persist before use, and single-
flight. Both have tests here that fail if the ordering or the lock is removed.
"""
import asyncio
import base64
import json
import time

import pytest

from apps.helper.credentials import CredentialError
from apps.helper.credentials.oauth import (
    CLIENT_ID,
    DEFAULT_CALLBACK_PORT,
    OwnOAuthSource,
    StoredTokens,
    TokenStore,
    authorize_url,
    callback_port_available,
    pkce_pair,
    redirect_uri,
    tokens_from_response,
)


def run(coro):
    return asyncio.run(coro)


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg':'none'})}.{seg(claims)}.sig"


def _tokens(*, exp_offset=86400, refresh="r-old", earliest=None):
    return StoredTokens(
        access_token=_jwt({"exp": time.time() + exp_offset}),
        refresh_token=refresh,
        id_token=_jwt({"https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-9", "chatgpt_plan_type": "pro"}}),
        expires_at=int(time.time() + exp_offset),
        earliest_refresh_at=earliest,
    )


class _FakeHttp:
    """Stands in for httpx.AsyncClient.post."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = 0

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls += 1
        payload, status = self.payload, self.status

        class _Res:
            status_code = status
            text = "err"

            def json(self):
                return payload

        await asyncio.sleep(0.01)  # let concurrent callers interleave
        return _Res()

    async def aclose(self):
        return None


# ---------- PKCE + authorize URL ----------


def test_pkce_challenge_is_s256_of_the_verifier():
    import hashlib

    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_verifier_is_fresh_each_time():
    assert pkce_pair()[0] != pkce_pair()[0]


def test_authorize_url_carries_the_required_params():
    url = authorize_url(port=1455, challenge="chal", state="st")
    for fragment in ("response_type=code", f"client_id={CLIENT_ID}",
                     "code_challenge=chal", "code_challenge_method=S256", "state=st"):
        assert fragment in url
    assert "localhost%3A1455%2Fauth%2Fcallback" in url


def test_redirect_uri_follows_the_port():
    assert redirect_uri(1456) == "http://localhost:1456/auth/callback"


def test_callback_port_detection_reports_a_busy_port():
    """A live `codex login` holds 1455; U1 leaves open whether another port is
    even accepted, so the collision is surfaced rather than worked around."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        assert callback_port_available(busy) is False
    assert callback_port_available(busy) is True


# ---------- storage ----------


def test_tokens_round_trip_through_the_store(tmp_path):
    store = TokenStore(tmp_path / "oauth.json")
    store.save(_tokens())
    loaded = store.load()
    assert loaded.refresh_token == "r-old"
    assert loaded.expires_at is not None


def test_absent_store_loads_as_none(tmp_path):
    assert TokenStore(tmp_path / "nope.json").load() is None


def test_corrupt_store_loads_as_none_rather_than_raising(tmp_path):
    p = tmp_path / "oauth.json"
    p.write_text("{not json")
    assert TokenStore(p).load() is None


def test_logout_clears_only_our_own_file(tmp_path):
    store = TokenStore(tmp_path / "oauth.json")
    store.save(_tokens())
    store.clear()
    assert store.load() is None
    store.clear()  # idempotent


def test_tokens_from_response_keeps_the_previous_refresh_when_omitted():
    prev = _tokens(refresh="keep-me")
    out = tokens_from_response({"access_token": _jwt({"exp": time.time() + 60})}, previous=prev)
    assert out.refresh_token == "keep-me"


def test_tokens_from_response_requires_an_access_token():
    with pytest.raises(CredentialError, match="no access_token"):
        tokens_from_response({})


# ---------- the source ----------


def test_absent_login_is_unavailable_with_an_actionable_remedy(tmp_path):
    src = OwnOAuthSource(TokenStore(tmp_path / "none.json"))
    assert src.available() is False
    with pytest.raises(CredentialError) as e:
        run(src.get())
    assert "login" in e.value.remedy


def test_valid_token_is_served_without_refreshing(tmp_path):
    http = _FakeHttp({})
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens())
    cred = run(OwnOAuthSource(store, http=http).get())
    assert http.calls == 0
    assert cred.headers["chatgpt-account-id"] == "acct-9"
    assert cred.plan == "pro"


def test_expiring_token_is_refreshed(tmp_path):
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10))  # inside the margin
    http = _FakeHttp({"access_token": _jwt({"exp": time.time() + 9999}),
                      "refresh_token": "r-new"})
    cred = run(OwnOAuthSource(store, http=http).get())
    assert http.calls == 1
    assert cred.token


def test_rotated_refresh_token_is_persisted_before_use(tmp_path):
    """Write-before-use: upstream has already invalidated the old refresh token,
    so if the new pair is not on disk the session is unrecoverable."""
    path = tmp_path / "o.json"
    store = TokenStore(path)
    store.save(_tokens(exp_offset=10, refresh="r-old"))
    http = _FakeHttp({"access_token": _jwt({"exp": time.time() + 9999}),
                      "refresh_token": "r-rotated"})
    run(OwnOAuthSource(store, http=http).get())
    assert TokenStore(path).load().refresh_token == "r-rotated"


def test_persist_failure_surfaces_rather_than_handing_out_a_doomed_token(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10))
    http = _FakeHttp({"access_token": _jwt({"exp": time.time() + 9999}),
                      "refresh_token": "r-new"})

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", boom)
    with pytest.raises(CredentialError, match="could not persist"):
        run(OwnOAuthSource(store, http=http).get())


def test_concurrent_gets_refresh_exactly_once(tmp_path):
    """Single-flight is a CORRECTNESS requirement under rotation: a second
    refresh would present a token upstream has already invalidated."""
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10))
    http = _FakeHttp({"access_token": _jwt({"exp": time.time() + 9999}),
                      "refresh_token": "r-new"})
    src = OwnOAuthSource(store, http=http)

    async def race():
        return await asyncio.gather(*(src.get() for _ in range(5)))

    creds = run(race())
    assert http.calls == 1
    assert len({c.token for c in creds}) == 1


def test_refresh_respects_the_upstream_floor(tmp_path):
    """The token endpoint reports earliest_refresh_at; refreshing early is
    rejected upstream, so we do not try."""
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10, earliest=int(time.time() + 3600)))
    http = _FakeHttp({})
    with pytest.raises(CredentialError, match="will not permit a refresh yet"):
        run(OwnOAuthSource(store, http=http).get())
    assert http.calls == 0


def test_missing_refresh_token_is_actionable(tmp_path):
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10, refresh=""))
    with pytest.raises(CredentialError) as e:
        run(OwnOAuthSource(store, http=_FakeHttp({})).get())
    assert "login" in e.value.remedy


def test_token_endpoint_error_is_actionable(tmp_path):
    store = TokenStore(tmp_path / "o.json")
    store.save(_tokens(exp_offset=10))
    with pytest.raises(CredentialError) as e:
        run(OwnOAuthSource(store, http=_FakeHttp({}, status=400)).get())
    assert "login" in e.value.remedy


def test_source_appears_in_the_default_chain_after_the_cli():
    from apps.helper.registry import default_registry

    names = [s.name for s in default_registry().get("codex").credentials]
    assert names == ["codex-cli", "helper-oauth"]
