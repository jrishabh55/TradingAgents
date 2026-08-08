"""The helper as a selectable provider in /api/config and graph construction.

The security-critical assertion here is that the helper credential never reaches
anything that gets serialized: store.create_run persists request.model_dump() as
config_json, so a token on RunRequest or in the graph config would be written to
SQLite in plaintext and would also reach SSE events and saved reports.
"""
import pytest

from apps.api.integrations import helper_backend
from apps.api.integrations.graph_factory import _build_config, build_graph_for_request
from apps.api.integrations.helper_backend import (
    HELPER_PROVIDER_KEY,
    HelperBackedGraph,
    helper_models,
    is_helper_provider,
)
from apps.api.routes.config import _PROVIDERS, get_config
from apps.api.schemas import RunRequest

ORIGINAL_KEYS = [key for _, key, _, _ in _PROVIDERS]


@pytest.fixture
def with_helper(tmp_path, monkeypatch):
    """A state dir containing a helper token, so the helper reads as enabled.

    Routing decisions probe the daemon for liveness; no daemon runs under
    pytest, so the probe is stubbed to "reachable" here.
    """
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))
    monkeypatch.delenv(helper_backend.HELPER_URL_ENV, raising=False)
    monkeypatch.delenv(helper_backend.HELPER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(helper_backend, "local_helper_reachable", lambda **kw: True)
    from apps.helper import paths

    paths.write_secret(paths.local_token_file(), "tok-abc123")
    return tmp_path


@pytest.fixture
def without_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv(helper_backend.HELPER_URL_ENV, raising=False)
    monkeypatch.delenv(helper_backend.HELPER_TOKEN_ENV, raising=False)
    return tmp_path


def _request(**over):
    body = dict(
        ticker="^NSEI",
        analysis_date="2026-08-01",
        analysts=["market"],
        research_depth=1,
        llm_provider="openai",
        shallow_thinker="gpt-5.4-mini",
        deep_thinker="gpt-5.4",
        backend_url=None,
    )
    body.update(over)
    return RunRequest(**body)


# ---------- /api/config ----------


def test_helper_is_offered_first_when_running(with_helper):
    c = get_config()
    assert c.providers[0].key == HELPER_PROVIDER_KEY
    assert c.models_by_provider[HELPER_PROVIDER_KEY]


def test_helper_is_listed_even_when_not_running(without_helper):
    """Always listed, flagged requires_helper: whether the user's helper is
    local, on the relay, or not installed yet is a runtime question the UI
    answers via /api/helper/status — hiding the provider would hide the setup
    path too. Runs without a helper are rejected at submit time instead."""
    c = get_config()
    assert c.providers[0].key == HELPER_PROVIDER_KEY
    assert c.providers[0].requires_helper is True
    assert [p.key for p in c.providers[1:]] == ORIGINAL_KEYS
    assert c.models_by_provider[HELPER_PROVIDER_KEY]


def test_existing_providers_are_untouched_by_the_addition(with_helper):
    """Adding the helper must not disturb the hand-maintained table — those
    backend URLs are deliberate (e.g. Qwen's China vs international endpoints
    use non-interchangeable credentials)."""
    c = get_config()
    assert [p.key for p in c.providers[1:]] == ORIGINAL_KEYS
    by_key = {p.key: p for p in c.providers}
    assert by_key["qwen"].backend_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert by_key["openai"].supports_reasoning_effort is True
    assert by_key["google"].supports_google_thinking is True


def test_helper_models_come_from_the_quirks_row_not_a_hardcoded_list(with_helper):
    """Model choice stays with the user; the list is derived."""
    from apps.helper.providers.codex import CODEX_QUIRKS

    ids = {m.id for m in get_config().models_by_provider[HELPER_PROVIDER_KEY]}
    assert CODEX_QUIRKS.valid_models <= ids
    assert set(CODEX_QUIRKS.aliases) <= ids


def test_helper_exposes_no_separate_effort_control(with_helper):
    """Effort rides in the model name for this provider."""
    p = get_config().providers[0]
    assert not (p.supports_reasoning_effort or p.supports_google_thinking
                or p.supports_anthropic_effort)


def test_helper_model_labels_are_all_populated(with_helper):
    assert all(m.label and m.id for m in helper_models() and get_config().models_by_provider[HELPER_PROVIDER_KEY])


# ---------- helper download ----------


def test_download_serves_the_built_artifact(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.routes.config import router

    artifact = tmp_path / "TradingAgentsHelper.zip"
    artifact.write_bytes(b"PK\x03\x04fake-zip")
    monkeypatch.setenv("TA_HELPER_DIST_FILE", str(artifact))

    a = FastAPI()
    a.include_router(router, prefix="/api")
    with TestClient(a) as c:
        r = c.get("/api/helper/download")
    assert r.status_code == 200
    assert r.content.startswith(b"PK")
    assert "TradingAgentsHelper.zip" in r.headers.get("content-disposition", "")


def test_download_404s_with_the_fix_when_no_build_exists(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.routes.config import router

    monkeypatch.setenv("TA_HELPER_DIST_FILE", str(tmp_path / "missing.zip"))
    a = FastAPI()
    a.include_router(router, prefix="/api")
    with TestClient(a) as c:
        r = c.get("/api/helper/download")
    assert r.status_code == 404
    assert "build.sh" in r.json()["detail"]


def test_download_picks_the_artifact_for_the_requesting_os(tmp_path, monkeypatch):
    """A Windows browser gets the installer, everyone else the DMG, and the
    generic zip backstops when no OS-specific build exists."""
    from apps.api.routes.config import _helper_dist_file

    monkeypatch.delenv("TA_HELPER_DIST_FILE", raising=False)
    monkeypatch.setenv("TA_HELPER_DIST_DIR", str(tmp_path))
    for name in ("TradingAgentsHelperSetup.exe", "TradingAgentsHelper.dmg",
                 "TradingAgentsHelper.zip"):
        (tmp_path / name).write_bytes(b"x")

    win_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    mac_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    assert _helper_dist_file(win_ua).name == "TradingAgentsHelperSetup.exe"
    assert _helper_dist_file(mac_ua).name == "TradingAgentsHelper.dmg"
    assert _helper_dist_file("").name == "TradingAgentsHelper.dmg"

    (tmp_path / "TradingAgentsHelperSetup.exe").unlink()
    (tmp_path / "TradingAgentsHelper.dmg").unlink()
    assert _helper_dist_file(win_ua).name == "TradingAgentsHelper.zip"
    assert _helper_dist_file(mac_ua).name == "TradingAgentsHelper.zip"


def test_status_advertises_the_download_when_a_build_exists(tmp_path, monkeypatch, without_helper):
    """The link appears the moment a build lands in dist/ — no env needed —
    and an explicit TA_HELPER_DOWNLOAD_URL (hosted artifact) still wins."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from apps.api.relay import reset_relay_registry_for_tests
    from apps.api.routes.config import router

    reset_relay_registry_for_tests()
    artifact = tmp_path / "TradingAgentsHelper.zip"
    artifact.write_bytes(b"PK")
    monkeypatch.setenv("TA_HELPER_DIST_FILE", str(artifact))
    monkeypatch.delenv("TA_HELPER_DOWNLOAD_URL", raising=False)

    a = FastAPI()

    @a.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = "u1"
        return await call_next(request)

    a.include_router(router, prefix="/api")
    with TestClient(a) as c:
        assert c.get("/api/helper/status").json()["download_url"] == "/api/helper/download"
        monkeypatch.setenv("TA_HELPER_DOWNLOAD_URL", "https://releases.example/helper.zip")
        assert c.get("/api/helper/status").json()["download_url"] == "https://releases.example/helper.zip"


# ---------- provider key mapping ----------


@pytest.mark.parametrize("value,expected", [
    ("chatgpt_helper", True), ("CHATGPT_HELPER", True), (" chatgpt_helper ", True),
    ("openai", False), ("openai_compatible", False), ("", False),
])
def test_is_helper_provider(value, expected):
    assert is_helper_provider(value) is expected


# ---------- graph construction ----------


def test_helper_provider_maps_onto_openai_compatible(with_helper, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    req = _request(llm_provider=HELPER_PROVIDER_KEY, shallow_thinker="ta-quick",
                   deep_thinker="ta-deep")
    graph, _, _, _ = build_graph_for_request(req)
    assert isinstance(graph, HelperBackedGraph)
    assert graph.config["llm_provider"] == "openai_compatible"
    assert graph.config["backend_url"].endswith("/v1/codex")


def test_relay_routes_through_the_shim_with_a_per_run_token(without_helper, monkeypatch):
    """No local helper, but the user's own helper is on the relay: the run must
    go through this server's shim, authenticated by a per-run internal token
    that maps back to the same user."""
    from apps.api.integrations.helper_backend import relay_shim_url
    from apps.api.relay import RelayConnection, reset_relay_registry_for_tests
    from apps.api.routes.relay import get_internal_tokens

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reg = reset_relay_registry_for_tests()
    reg.register(RelayConnection("u1", lambda m: None))

    req = _request(llm_provider=HELPER_PROVIDER_KEY, shallow_thinker="ta-quick",
                   deep_thinker="ta-deep")
    graph, _, _, _ = build_graph_for_request(req, user_id="u1")
    assert graph.config["backend_url"] == relay_shim_url()
    token = graph._relay_token
    assert token and get_internal_tokens().resolve(token) == "u1"
    assert graph._get_provider_kwargs()["api_key"] == token
    get_internal_tokens().revoke(token)


def test_no_helper_anywhere_refuses_to_build(without_helper, monkeypatch):
    from apps.api.relay import reset_relay_registry_for_tests

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_relay_registry_for_tests()
    req = _request(llm_provider=HELPER_PROVIDER_KEY, shallow_thinker="ta-quick",
                   deep_thinker="ta-deep")
    with pytest.raises(RuntimeError, match="not connected"):
        build_graph_for_request(req, user_id="u1")


def test_helper_ready_reflects_local_or_relay(without_helper):
    from apps.api.integrations.helper_backend import helper_ready
    from apps.api.relay import RelayConnection, reset_relay_registry_for_tests

    reg = reset_relay_registry_for_tests()
    assert helper_ready("u1") is False
    reg.register(RelayConnection("u1", lambda m: None))
    assert helper_ready("u1") is True
    assert helper_ready("someone-else") is False


def test_a_client_supplied_backend_url_is_ignored_for_the_helper(with_helper, monkeypatch):
    """request.backend_url is client input. Honouring it here would attach the
    helper's bearer credential to an arbitrary caller-chosen host, exfiltrating
    the token and every prompt."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    req = _request(llm_provider=HELPER_PROVIDER_KEY, shallow_thinker="ta-quick",
                   deep_thinker="ta-deep", backend_url="https://attacker.example/v1")
    graph, _, _, _ = build_graph_for_request(req)
    assert "attacker.example" not in graph.config["backend_url"]
    assert graph.config["backend_url"].endswith("/v1/codex")


def test_non_helper_provider_builds_the_plain_graph(with_helper, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    graph, _, _, _ = build_graph_for_request(_request(llm_provider="openai"))
    assert not isinstance(graph, HelperBackedGraph)
    assert graph.config["llm_provider"] == "openai"


def test_credential_reaches_provider_kwargs_but_not_config(with_helper, monkeypatch):
    """The whole point: the key must be injectable without being serializable."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    req = _request(llm_provider=HELPER_PROVIDER_KEY, shallow_thinker="ta-quick",
                   deep_thinker="ta-deep")
    graph, _, _, _ = build_graph_for_request(req)
    assert graph._get_provider_kwargs()["api_key"] == "tok-abc123"
    assert "tok-abc123" not in str(graph.config)


def test_the_token_never_appears_in_the_persisted_request(with_helper):
    """store.create_run writes request.model_dump() to config_json."""
    req = _request(llm_provider=HELPER_PROVIDER_KEY)
    dumped = str(req.model_dump())
    assert "tok-abc123" not in dumped
    assert "api_key" not in dumped


def test_build_config_never_carries_a_credential(with_helper):
    cfg = _build_config(_request(llm_provider=HELPER_PROVIDER_KEY), user_id="u1")
    assert "api_key" not in cfg
    assert "tok-abc123" not in str(cfg)
