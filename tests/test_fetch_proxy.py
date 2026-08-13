"""Residential fetch-proxy plumbing.

Three layers under test:
- tradingagents/dataflows/fetch_proxy.py — the urlopen wrapper + resolver hook
- apps/api/integrations/fetch_proxy.py — per-run thread-local activation
- apps/api/routes/relay.py::relay_fetch_shim — the internal HTTP bridge
"""
from __future__ import annotations

import base64
import json
import threading
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tradingagents.dataflows import fetch_proxy as upstream


@pytest.fixture(autouse=True)
def _clean_resolver():
    upstream.set_resolver(None)
    yield
    upstream.set_resolver(None)


def _shim_resp(payload: dict):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Resp()


def _req() -> Request:
    return Request("https://www.reddit.com/r/stocks/search.rss?q=NVDA",
                   headers={"User-Agent": "tradingagents/0.2"})


# ---------- upstream wrapper ----------


@pytest.mark.unit
class TestUrlopenMaybeProxied:
    def test_no_resolver_uses_direct_opener(self):
        direct_calls = []
        upstream.urlopen_maybe_proxied(
            _req(), timeout=5.0, direct=lambda req, timeout: direct_calls.append(req) or "direct"
        )
        assert len(direct_calls) == 1

    def test_active_proxy_posts_envelope_and_returns_body(self):
        upstream.set_resolver(lambda: upstream.FetchProxy("http://self/internal/relay/fetch", "tok-1"))
        seen = {}

        def fake_urlopen(shim_req, timeout=None):
            seen["url"] = shim_req.full_url
            seen["auth"] = shim_req.headers.get("Authorization")
            seen["envelope"] = json.loads(shim_req.data)
            return _shim_resp({"ok": True, "status": 200,
                               "body_b64": base64.b64encode(b"<feed/>").decode()})

        with patch.object(upstream, "urlopen", fake_urlopen):
            with upstream.urlopen_maybe_proxied(_req(), timeout=5.0) as resp:
                assert resp.read() == b"<feed/>" and resp.status == 200
        assert seen["url"] == "http://self/internal/relay/fetch"
        assert seen["auth"] == "Bearer tok-1"
        assert seen["envelope"]["url"].startswith("https://www.reddit.com/")
        assert seen["envelope"]["headers"]["User-agent"] == "tradingagents/0.2"

    def test_proxied_non_2xx_raises_httperror_like_a_direct_fetch(self):
        upstream.set_resolver(lambda: upstream.FetchProxy("http://self/f", "t"))
        with patch.object(upstream, "urlopen",
                          lambda *a, **k: _shim_resp({"ok": True, "status": 429, "body_b64": ""})):
            with pytest.raises(HTTPError) as exc_info:
                upstream.urlopen_maybe_proxied(_req(), timeout=5.0)
        assert exc_info.value.code == 429

    def test_proxy_transport_failure_falls_back_to_direct(self):
        upstream.set_resolver(lambda: upstream.FetchProxy("http://self/f", "t"))
        direct_calls = []

        def broken_shim(*a, **k):
            raise OSError("shim down")

        with patch.object(upstream, "urlopen", broken_shim):
            out = upstream.urlopen_maybe_proxied(
                _req(), timeout=5.0,
                direct=lambda req, timeout: direct_calls.append(req) or "direct",
            )
        assert out == "direct" and len(direct_calls) == 1

    def test_helper_rejection_falls_back_to_direct(self):
        """ok:false (allowlist reject, size cap, helper network error) means
        'proxy could not do it' — never an error surfaced to the analyst."""
        upstream.set_resolver(lambda: upstream.FetchProxy("http://self/f", "t"))
        with patch.object(upstream, "urlopen",
                          lambda *a, **k: _shim_resp({"ok": False, "error": "host not allowed"})):
            out = upstream.urlopen_maybe_proxied(
                _req(), timeout=5.0, direct=lambda req, timeout: "direct"
            )
        assert out == "direct"

    def test_broken_resolver_is_ignored(self):
        upstream.set_resolver(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        out = upstream.urlopen_maybe_proxied(_req(), timeout=5.0,
                                             direct=lambda req, timeout: "direct")
        assert out == "direct"


# ---------- per-run thread-local activation ----------


@pytest.mark.unit
class TestRunActivation:
    def test_activation_is_thread_scoped(self):
        from apps.api.integrations import fetch_proxy as ctx
        from apps.api.relay import RelayConnection, reset_relay_registry_for_tests
        from apps.api.routes.relay import get_internal_tokens

        reg = reset_relay_registry_for_tests()
        reg.register(RelayConnection("u1", lambda m: None))

        token = ctx.activate_for_run("u1")
        try:
            assert token and get_internal_tokens().resolve(token) == "u1"
            proxy = upstream._current_proxy()
            assert proxy is not None and proxy.token == token
            assert proxy.url.endswith("/internal/relay/fetch")

            other_thread_proxy = []
            t = threading.Thread(
                target=lambda: other_thread_proxy.append(upstream._current_proxy())
            )
            t.start(); t.join()
            assert other_thread_proxy == [None]  # other threads stay unproxied
        finally:
            ctx.deactivate(token)
        assert upstream._current_proxy() is None
        assert get_internal_tokens().resolve(token) is None

    def test_no_helper_means_no_proxy_and_no_token(self):
        from apps.api.integrations import fetch_proxy as ctx
        from apps.api.relay import reset_relay_registry_for_tests

        reset_relay_registry_for_tests()
        assert ctx.activate_for_run("u1") is None
        assert upstream._current_proxy() is None
        ctx.deactivate(None)  # must be safe


# ---------- the internal shim route ----------


def _shim_client(monkeypatch, dispatch):
    from apps.api.routes import relay as relay_routes

    class _Registry:
        async def dispatch(self, user_id, provider, body, *, timeout_s):
            return await dispatch(user_id, provider, body, timeout_s)

    monkeypatch.setattr(relay_routes, "get_relay_registry", lambda: _Registry())
    app = FastAPI()
    app.include_router(relay_routes.router)
    return TestClient(app)


@pytest.mark.unit
class TestFetchShim:
    def test_requires_a_valid_internal_token(self, monkeypatch):
        async def _never(*a):  # pragma: no cover
            raise AssertionError

        client = _shim_client(monkeypatch, _never)
        r = client.post("/internal/relay/fetch", json={"url": "https://x"},
                        headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_bridges_to_the_users_helper(self, monkeypatch):
        from apps.api.routes.relay import get_internal_tokens

        seen = {}

        async def fake_dispatch(user_id, provider, body, timeout_s):
            seen.update(user=user_id, provider=provider, body=body, timeout=timeout_s)
            return {"ok": True, "status": 200, "body_b64": ""}

        client = _shim_client(monkeypatch, fake_dispatch)
        token = get_internal_tokens().mint("u1")
        try:
            r = client.post("/internal/relay/fetch",
                            json={"url": "https://www.reddit.com/x"},
                            headers={"Authorization": f"Bearer {token}"})
        finally:
            get_internal_tokens().revoke(token)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["user"] == "u1" and seen["provider"] == "__fetch__"
        assert seen["timeout"] < 900  # fetch-sized, not LLM-sized

    def test_helper_offline_is_503_ok_false(self, monkeypatch):
        from apps.api.relay import RelayUnavailable
        from apps.api.routes.relay import get_internal_tokens

        async def gone(*a):
            raise RelayUnavailable("no helper connected")

        client = _shim_client(monkeypatch, gone)
        token = get_internal_tokens().mint("u1")
        try:
            r = client.post("/internal/relay/fetch", json={"url": "https://x"},
                            headers={"Authorization": f"Bearer {token}"})
        finally:
            get_internal_tokens().revoke(token)
        assert r.status_code == 503 and r.json()["ok"] is False
