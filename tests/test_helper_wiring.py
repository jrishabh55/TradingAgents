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
    """A state dir containing a helper token, so the helper reads as enabled."""
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))
    monkeypatch.delenv(helper_backend.HELPER_URL_ENV, raising=False)
    monkeypatch.delenv(helper_backend.HELPER_TOKEN_ENV, raising=False)
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


def test_helper_is_invisible_when_not_running(without_helper):
    c = get_config()
    assert [p.key for p in c.providers] == ORIGINAL_KEYS
    assert HELPER_PROVIDER_KEY not in c.models_by_provider


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
