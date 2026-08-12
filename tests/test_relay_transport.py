"""Relay transport: the websocket endpoint, the loopback shim, and the client.

The end-to-end test here is the one that matters — a real WebSocket carrying a
real Chat Completions request from the shim to a helper and back, with only the
upstream adapter faked.
"""
import asyncio
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.relay import reset_relay_registry_for_tests
from apps.api.routes.relay import get_internal_tokens, router as relay_router
from apps.helper.types import NormalizedResponse, ToolCall, Usage


@pytest.fixture
def app(monkeypatch):
    for var in ("CLERK_JWKS_URL", "CLERK_ISSUER", "WEBAPP_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # The websocket refuses open (unauthenticated) mode outright — an
    # unauthenticated registration could displace a real helper — so the
    # fixture runs in shared-bearer mode. Individual tests override.
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "relay-test-token")
    from apps.api.auth import reset_verifier_for_tests

    reset_verifier_for_tests()
    reset_relay_registry_for_tests()
    a = FastAPI()
    a.include_router(relay_router)
    return a


#: Authenticated websocket path for the fixture's shared-bearer mode.
WS = "/api/relay/ws?token=relay-test-token"

BODY = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hi"}]}


def _mint(user="shared-bearer"):
    return get_internal_tokens().mint(user)


# ---------- internal tokens ----------


def test_internal_token_resolves_to_its_user():
    reg = get_internal_tokens()
    tok = reg.mint("user_42")
    assert reg.resolve(tok) == "user_42"
    reg.revoke(tok)
    assert reg.resolve(tok) is None


def test_unknown_internal_token_resolves_to_nothing():
    assert get_internal_tokens().resolve("nonsense") is None


def test_internal_tokens_are_unguessable():
    reg = get_internal_tokens()
    assert len({reg.mint("u") for _ in range(20)}) == 20
    assert all(len(reg.mint("u")) > 30 for _ in range(3))


# ---------- shim authentication ----------


def test_shim_rejects_a_missing_token(app):
    with TestClient(app) as c:
        r = c.post("/internal/relay/v1/codex/chat/completions", json=BODY)
    assert r.status_code == 401


def test_shim_rejects_a_bogus_token(app):
    with TestClient(app) as c:
        r = c.post("/internal/relay/v1/codex/chat/completions", json=BODY,
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_shim_reports_503_when_no_helper_is_connected(app):
    """Distinct from a 500: the run can succeed once the helper reconnects."""
    tok = _mint()
    with TestClient(app) as c:
        r = c.post("/internal/relay/v1/codex/chat/completions", json=BODY,
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "helper_disconnected"


def test_shim_rejects_a_non_json_body(app):
    tok = _mint()
    with TestClient(app) as c:
        r = c.post("/internal/relay/v1/codex/chat/completions", content=b"not json",
                   headers={"Authorization": f"Bearer {tok}",
                            "Content-Type": "application/json"})
    assert r.status_code == 400


def test_the_shim_is_not_under_the_api_prefix(app):
    """The frontend proxies /api/* — the shim must not be reachable that way."""
    paths = [r.path for r in app.routes if "relay" in getattr(r, "path", "")]
    assert "/internal/relay/v1/{provider}/chat/completions" in paths
    assert not any(p.startswith("/api/internal") for p in paths)


# ---------- websocket lifecycle ----------


def test_socket_accepts_and_announces_ready(app):
    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            # The helper's UI shows whose runs this connection serves; a wrong
            # pairing must be visible, not a silent "connected".
            assert ready["user_id"] == "shared-bearer"


def test_socket_registers_the_user(app):
    from apps.api.relay import get_relay_registry

    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ws.receive_json()
            assert get_relay_registry().is_connected("shared-bearer")
    # Unregistered on close.
    assert not get_relay_registry().is_connected("shared-bearer")


def test_socket_answers_ping(app):
    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ws.receive_json()
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_socket_records_advertised_providers(app):
    from apps.api.relay import get_relay_registry

    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ws.receive_json()
            ws.send_json({"type": "hello", "providers": ["codex", "openai"]})
            ws.send_json({"type": "ping"})
            ws.receive_json()
            assert get_relay_registry().get("shared-bearer").providers == ["codex", "openai"]


def test_socket_rejects_when_clerk_is_configured_without_a_token(app, monkeypatch):
    monkeypatch.setenv("CLERK_JWKS_URL", "https://example/.well-known/jwks.json")
    from apps.api.auth import reset_verifier_for_tests

    reset_verifier_for_tests()
    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            assert ws.receive_json() == {"type": "error", "error": "unauthorized"}


def test_socket_accepts_the_legacy_shared_bearer(app, monkeypatch):
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "shhh")
    with TestClient(app) as c:
        with c.websocket_connect("/api/relay/ws?token=shhh") as ws:
            assert ws.receive_json()["type"] == "ready"


def test_socket_rejects_a_wrong_legacy_bearer(app, monkeypatch):
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "shhh")
    with TestClient(app) as c:
        with c.websocket_connect("/api/relay/ws?token=wrong") as ws:
            assert ws.receive_json()["type"] == "error"


def test_pair_tokens_round_trip_and_revoke(tmp_path):
    from apps.api.jobs.store import JobStore

    store = JobStore(tmp_path / "runs.sqlite")
    raw = store.create_pair_token("user_42")
    assert raw.startswith("tarelay_")
    assert store.resolve_pair_token(raw) == "user_42"
    assert store.resolve_pair_token("tarelay_forged") is None
    # Only the hash is stored — the raw token appears nowhere in the DB.
    import sqlite3

    rows = sqlite3.connect(tmp_path / "runs.sqlite").execute(
        "SELECT token_hash FROM relay_pair_tokens"
    ).fetchall()
    assert rows and all(raw not in r[0] for r in rows)
    assert store.revoke_pair_tokens("user_42") == 1
    assert store.resolve_pair_token(raw) is None


def test_socket_accepts_a_pairing_token_as_the_user(app, tmp_path, monkeypatch):
    """The credential an installed helper daemon holds: no browser, no JWT."""
    from apps.api.jobs.store import JobStore
    from apps.api.relay import get_relay_registry

    store = JobStore(tmp_path / "runs.sqlite")
    monkeypatch.setattr("apps.api.jobs.store.get_store", lambda: store)
    raw = store.create_pair_token("user_42")
    with TestClient(app) as c:
        with c.websocket_connect(f"/api/relay/ws?token={raw}") as ws:
            assert ws.receive_json()["type"] == "ready"
            assert get_relay_registry().is_connected("user_42")


def test_pair_endpoint_mints_a_usable_command(tmp_path, monkeypatch):
    from apps.api.jobs.store import JobStore

    store = JobStore(tmp_path / "runs.sqlite")
    monkeypatch.setattr("apps.api.jobs.store.get_store", lambda: store)
    # Pairing is refused in open mode; any configured auth mode allows it.
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")

    a = FastAPI()

    @a.middleware("http")
    async def fake_auth(request, call_next):  # noqa: ANN001
        request.state.user_id = "user_42"
        return await call_next(request)

    a.include_router(relay_router)
    with TestClient(a) as c:
        r = c.post("/api/relay/pair")
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("tarelay_")
    assert body["ws_url"].endswith("/api/relay/ws")
    assert body["token"] in body["command"] and body["ws_url"] in body["command"]
    assert store.resolve_pair_token(body["token"]) == "user_42"


def test_pair_endpoint_refuses_open_mode(tmp_path, monkeypatch):
    """Minting in open mode would hand any caller a token the ws endpoint
    accepts — bypassing its refusal of unauthenticated registration."""
    for var in ("CLERK_JWKS_URL", "CLERK_ISSUER", "WEBAPP_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    from apps.api.auth import reset_verifier_for_tests

    reset_verifier_for_tests()

    a = FastAPI()

    @a.middleware("http")
    async def fake_auth(request, call_next):  # noqa: ANN001
        request.state.user_id = "anonymous"
        return await call_next(request)

    a.include_router(relay_router)
    with TestClient(a) as c:
        r = c.post("/api/relay/pair")
    assert r.status_code == 403


def test_pair_ws_url_prefers_the_public_origin(tmp_path, monkeypatch):
    """Behind the frontend proxy request.url is an internal hostname; the
    command must carry the address the USER'S machine can reach."""
    from apps.api.jobs.store import JobStore

    store = JobStore(tmp_path / "runs.sqlite")
    monkeypatch.setattr("apps.api.jobs.store.get_store", lambda: store)
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "s3cret")
    monkeypatch.setenv("WEBAPP_PUBLIC_URL", "https://trade.example.com")

    a = FastAPI()

    @a.middleware("http")
    async def fake_auth(request, call_next):  # noqa: ANN001
        request.state.user_id = "user_42"
        return await call_next(request)

    a.include_router(relay_router)
    with TestClient(a) as c:
        r = c.post("/api/relay/pair")
    assert r.json()["ws_url"] == "wss://trade.example.com/api/relay/ws"


def test_socket_refuses_open_mode_entirely(app, monkeypatch):
    """No Clerk, no shared bearer → refuse. Registration is last-writer-wins
    per user, so an open socket would let any client displace the real helper
    and intercept that user's LLM traffic."""
    monkeypatch.delenv("WEBAPP_AUTH_TOKEN", raising=False)
    with TestClient(app) as c:
        with c.websocket_connect("/api/relay/ws") as ws:
            assert ws.receive_json() == {"type": "error", "error": "unauthorized"}


# ---------- end to end through a real socket ----------


def test_a_request_travels_shim_to_helper_and_back(app):
    """The whole point: an HTTP request on the server becomes an LLM call
    executed on the helper's machine, and the answer comes back."""
    done = threading.Event()
    captured: dict = {}

    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ws.receive_json()  # ready

            def helper_loop():
                msg = ws.receive_json()
                captured["call"] = msg
                ws.send_json({
                    "type": "result",
                    "id": msg["id"],
                    "payload": {"status": 200, "body": {"id": "chatcmpl-x",
                                                       "choices": [{"index": 0,
                                                                    "message": {"role": "assistant",
                                                                                "content": "42"},
                                                                    "finish_reason": "stop"}]}},
                })
                done.set()

            t = threading.Thread(target=helper_loop, daemon=True)
            t.start()

            tok = _mint()
            r = c.post("/internal/relay/v1/codex/chat/completions", json=BODY,
                       headers={"Authorization": f"Bearer {tok}"})
            done.wait(timeout=5)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "42"
    assert captured["call"]["provider"] == "codex"
    assert captured["call"]["body"]["model"] == "gpt-5.6-luna"


def test_an_upstream_error_status_is_passed_through(app):
    with TestClient(app) as c:
        with c.websocket_connect(WS) as ws:
            ws.receive_json()

            def helper_loop():
                msg = ws.receive_json()
                ws.send_json({"type": "result", "id": msg["id"],
                              "payload": {"status": 429,
                                          "body": {"error": {"message": "slow down"}}}})

            threading.Thread(target=helper_loop, daemon=True).start()
            tok = _mint()
            r = c.post("/internal/relay/v1/codex/chat/completions", json=BODY,
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 429
    assert r.json()["error"]["message"] == "slow down"


# ---------- helper-side client ----------


def _fake_provider(response=None, exc=None):
    from apps.helper.quirks import ProviderQuirks
    from apps.helper.registry import Provider

    class _Adapter:
        name = "fake"

        async def send(self, req, cred, quirks, ctx):
            if exc is not None:
                raise exc
            return response

    class _Cred:
        name = "fake-cred"

        async def get(self):
            from apps.helper.credentials import Credential

            return Credential(token="t")

    return Provider(name="codex", adapter=_Adapter(), quirks=ProviderQuirks(),
                    credentials=(_Cred(),))


def _client_with(provider):
    from apps.helper.registry import Registry
    from apps.helper.relay_client import RelayClient

    return RelayClient("ws://unused", "tok", registry=Registry([provider]))


def test_client_executes_a_call_and_renders_chat_completions():
    resp = NormalizedResponse(model="m", content="hello", usage=Usage(1, 2, 0))
    client = _client_with(_fake_provider(response=resp))
    out = asyncio.run(client._execute("codex", BODY))
    assert out["status"] == 200
    assert out["body"]["choices"][0]["message"]["content"] == "hello"


def test_client_renders_tool_calls():
    resp = NormalizedResponse(model="m", tool_calls=[ToolCall("c1", "get_x", "{}")],
                              finish_reason="tool_calls")
    out = asyncio.run(_client_with(_fake_provider(response=resp))._execute("codex", BODY))
    msg = out["body"]["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"][0]["function"]["name"] == "get_x"


def test_client_rejects_an_unknown_provider():
    out = asyncio.run(_client_with(_fake_provider())._execute("nope", BODY))
    assert out["status"] == 400


def test_client_refuses_streaming_over_the_relay():
    out = asyncio.run(_client_with(_fake_provider())._execute("codex", {**BODY, "stream": True}))
    assert out["status"] == 501


def test_client_surfaces_a_credential_problem_with_its_remedy():
    """The server cannot fix this — the user must act on their own machine."""
    from apps.helper.credentials import CredentialError
    from apps.helper.quirks import ProviderQuirks
    from apps.helper.registry import Provider

    class _Cred:
        name = "broken"

        async def get(self):
            raise CredentialError("token expired", remedy="Run `codex login`.")

    class _Adapter:
        name = "fake"

        async def send(self, *a):
            raise AssertionError("must not be reached")

    provider = Provider(name="codex", adapter=_Adapter(), quirks=ProviderQuirks(),
                        credentials=(_Cred(),))
    out = asyncio.run(_client_with(provider)._execute("codex", BODY))
    assert out["status"] == 401
    assert "codex login" in out["body"]["error"]["message"]


def test_client_maps_a_helper_error_to_its_status():
    from apps.helper.types import RateLimited

    out = asyncio.run(_client_with(_fake_provider(exc=RateLimited("slow")))._execute("codex", BODY))
    assert out["status"] == 429


def test_client_reports_a_bad_request_body():
    out = asyncio.run(_client_with(_fake_provider())._execute("codex", {"messages": []}))
    assert out["status"] == 400
