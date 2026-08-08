"""The helper's desktop-app surface: local UI, its API, and the login flow.

The security property under test: /ui is the ONLY unauthenticated page (it
carries no secrets), every /ui/api route needs the local bearer token, and the
origin guard admits exactly our own page's same-origin requests.
"""
import asyncio
import json
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from apps.helper.server import RelayManager, create_app

TOKEN = "tok-test"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------- the page and its auth boundary ----------


def test_ui_page_is_served_without_auth_and_carries_no_secrets(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "Drishti Helper" in r.text
    assert TOKEN not in r.text


def test_ui_api_requires_the_bearer_token(client):
    assert client.get("/ui/api/state").status_code == 401
    assert client.get("/ui/api/state", headers=_auth()).status_code == 200


def test_same_origin_requests_are_allowed(client):
    """Our own page's POSTs carry a same-origin Origin header."""
    r = client.post(
        "/ui/api/relay",
        headers={**_auth(), "Origin": "http://testserver"},
        json={"url": "", "token": ""},
    )
    assert r.status_code == 400  # past the guard; rejected on content


def test_cross_origin_requests_are_still_rejected(client):
    r = client.get(
        "/ui/api/state",
        headers={**_auth(), "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_state_reports_login_and_relay_sections(client):
    body = client.get("/ui/api/state", headers=_auth()).json()
    assert body["login"] == {"status": "idle", "error": ""}
    assert body["relay"] == {"configured": False, "url": "", "connected": False,
                             "error": "", "user": ""}
    assert "providers" in body


def test_state_reports_autostart_and_update_sections(client):
    from apps.helper.version import __version__

    body = client.get("/ui/api/state", headers=_auth()).json()
    assert body["autostart"] == {"enabled": False}
    # No relay configured -> no portal to ask -> no update on offer.
    assert body["update"]["current"] == __version__
    assert body["update"]["available"] is False


# ---------- start at login ----------


def test_autostart_toggle_roundtrip(client, tmp_path, monkeypatch):
    # Point every platform's target under tmp_path so the test never touches
    # the real LaunchAgents / Startup / autostart directories.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from apps.helper import autostart

    assert not autostart.enabled()
    r = client.post("/ui/api/autostart", headers=_auth(), json={"enabled": True})
    assert r.json() == {"enabled": True}
    assert autostart.enabled()
    # The registration launches headless — a window at every login is rude.
    assert "--no-browser" in autostart._target().read_bytes().decode("utf-8", "replace")

    r = client.post("/ui/api/autostart", headers=_auth(), json={"enabled": False})
    assert r.json() == {"enabled": False}
    assert not autostart.enabled()


def test_autostart_rejects_non_boolean(client):
    r = client.post("/ui/api/autostart", headers=_auth(), json={"enabled": "yes"})
    assert r.status_code == 400


# ---------- update check ----------


def test_update_offered_when_portal_reports_newer_version(client, monkeypatch):
    import apps.helper.server as srv

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"version": "9.9.9", "download_url": "/api/helper/download"}

    class _Http:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            assert url == "https://portal.example/api/helper/version"
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Http)
    # A configured relay is what tells the helper which portal to ask.
    monkeypatch.setattr(
        srv.RelayManager, "load_config",
        lambda self: {"url": "wss://portal.example/api/relay/ws", "token": "t"},
    )

    up = client.get("/ui/api/state", headers=_auth()).json()["update"]
    assert up["available"] is True
    assert up["latest"] == "9.9.9"
    # Relative download paths resolve against the portal origin.
    assert up["download_url"] == "https://portal.example/api/helper/download"


# ---------- relay manager ----------


class _FakeClient:
    instances: list = []

    def __init__(self, url, token, registry=None):
        self.url, self.token = url, token
        self.connected = False
        self.last_error = ""
        self.remote_user = ""
        self.stopped = False
        _FakeClient.instances.append(self)

    async def run_forever(self):
        self.connected = True
        try:
            await asyncio.Event().wait()  # runs until cancelled
        finally:
            self.connected = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_relay_client(monkeypatch):
    _FakeClient.instances = []
    import apps.helper.relay_client as rc

    monkeypatch.setattr(rc, "RelayClient", _FakeClient)
    return _FakeClient


def test_connect_persists_config_and_reconnects_on_startup(tmp_path, monkeypatch, fake_relay_client):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))

    async def scenario():
        from apps.helper.registry import Registry

        mgr = RelayManager(Registry([]))
        await mgr.start("wss://portal.example/api/relay/ws", "tarelay_abc")
        await asyncio.sleep(0)  # let run_forever set connected
        assert mgr.state()["connected"] is True
        assert mgr.state()["url"] == "wss://portal.example/api/relay/ws"
        await mgr.stop()

        # A fresh manager (process restart) picks the config back up.
        mgr2 = RelayManager(Registry([]))
        await mgr2.autostart()
        await asyncio.sleep(0)
        assert mgr2.state()["connected"] is True
        assert fake_relay_client.instances[-1].token == "tarelay_abc"
        await mgr2.stop()

    asyncio.run(scenario())


def test_forget_removes_the_stored_config(tmp_path, monkeypatch, fake_relay_client):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))

    async def scenario():
        from apps.helper.registry import Registry

        mgr = RelayManager(Registry([]))
        await mgr.start("wss://portal.example/ws", "tarelay_abc")
        await mgr.stop(forget=True)
        assert mgr.load_config() is None
        assert mgr.state()["configured"] is False

    asyncio.run(scenario())


# ---------- login flow ----------


def test_login_flow_completes_via_the_callback(tmp_path, monkeypatch):
    from apps.helper import login_flow as lf
    from apps.helper.credentials.oauth import StoredTokens, TokenStore

    saved = {}

    async def fake_exchange(code, verifier, *, port, http=None):
        saved["code"] = code
        return StoredTokens(access_token="at-1", refresh_token="rt-1")

    monkeypatch.setattr(lf, "exchange_code", fake_exchange)
    store = TokenStore(tmp_path / "oauth.json")

    async def scenario():
        flow = lf.LoginFlow(store)
        url = flow.start(port=18455, timeout_s=10)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        assert flow.status == "pending"
        # Simulate the browser redirect hitting the loopback callback.
        await asyncio.to_thread(
            urllib.request.urlopen,
            f"http://127.0.0.1:18455/auth/callback?code=c-1&state={state}",
        )
        await flow._task
        assert flow.status == "done", flow.error
        assert saved["code"] == "c-1"
        assert store.load().access_token == "at-1"

    asyncio.run(scenario())


def test_login_flow_rejects_a_foreign_state(tmp_path, monkeypatch):
    from apps.helper import login_flow as lf
    from apps.helper.credentials.oauth import TokenStore

    async def scenario():
        flow = lf.LoginFlow(TokenStore(tmp_path / "oauth.json"))
        flow.start(port=18456, timeout_s=10)
        await asyncio.to_thread(
            urllib.request.urlopen,
            "http://127.0.0.1:18456/auth/callback?code=c-1&state=WRONG",
        )
        await flow._task
        assert flow.status == "error"
        assert "state" in flow.error

    asyncio.run(scenario())


def test_ui_login_endpoint_reports_busy_ports_cleanly(client, monkeypatch):
    import apps.helper.login_flow as lf

    monkeypatch.setattr(lf, "callback_port_available", lambda port: False)
    r = client.post("/ui/api/login", headers=_auth())
    assert r.status_code == 409
    assert "busy" in r.json()["error"]["message"]
