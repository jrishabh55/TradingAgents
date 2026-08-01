"""Guard that the API's initial state matches what upstream's propagate() builds.

The runner streams ``graph.graph.stream()`` directly instead of calling
``TradingAgentsGraph.propagate()``, so ``graph_factory`` has to reproduce
``_run_graph``'s state wiring itself. These tests fail if that wiring drifts —
either because we stop passing a field or because upstream adds one.
"""
import inspect
from unittest.mock import patch

import pytest

from apps.api.integrations.graph_factory import (
    build_graph_for_request,
    effective_analysts,
)
from apps.api.schemas import RunRequest
from tradingagents.graph.propagation import Propagator


def _request(ticker: str, analysts=("market",)) -> RunRequest:
    return RunRequest(
        ticker=ticker,
        analysis_date="2026-07-01",
        analysts=list(analysts),
        research_depth=1,
        llm_provider="openai",
        shallow_thinker="gpt-5.4-mini",
        deep_thinker="gpt-5.4",
        backend_url="https://api.openai.com/v1",
    )


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch):
    """Graph construction builds LLM clients, which want a key present."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")


@pytest.fixture(autouse=True)
def _offline_identity():
    """Keep the yfinance identity lookup out of the test path.

    Patched at the trading_graph import site so the resolved-identity branch of
    build_instrument_context still runs on a known payload.
    """
    identity = {
        "company_name": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NMS",
    }
    with patch(
        "tradingagents.graph.trading_graph.resolve_instrument_identity",
        return_value=identity,
    ):
        yield


@pytest.mark.parametrize(
    "ticker,expected_asset_type",
    [
        ("RELIANCE.NS", "stock"),
        ("AAPL", "stock"),
        ("BTC-USD", "crypto"),
        ("BTC-USDT", "crypto"),
    ],
)
def test_asset_type_is_detected_from_the_ticker(ticker, expected_asset_type):
    _, init_state, _, _ = build_graph_for_request(_request(ticker))
    assert init_state["asset_type"] == expected_asset_type


def test_instrument_context_carries_the_resolved_identity():
    _, init_state, _, _ = build_graph_for_request(_request("AAPL"))
    context = init_state["instrument_context"]
    assert "Apple Inc." in context
    assert "Do not substitute a different company" in context
    assert "`AAPL`" in context


def test_crypto_context_does_not_frame_the_asset_as_a_company():
    _, init_state, _, _ = build_graph_for_request(_request("BTC-USD"))
    context = init_state["instrument_context"]
    assert "asset to analyze" in context
    assert "crypto asset rather than a company" in context


_ALL = ["market", "social", "news", "fundamentals"]


def test_crypto_drops_the_fundamentals_analyst():
    """A coin has no balance sheet; the CLI drops it, so the API must too."""
    assert effective_analysts(_request("BTC-USD", _ALL)) == ["market", "social", "news"]


def test_stocks_keep_every_requested_analyst():
    assert effective_analysts(_request("RELIANCE.NS", _ALL)) == _ALL


def test_graph_and_translator_agree_on_the_analyst_list():
    """The UI builds its panels from this list — a mismatch hangs a panel."""
    request = _request("BTC-USD", _ALL)
    graph, _, _, _ = build_graph_for_request(request)
    assert list(graph.selected_analysts) == effective_analysts(request)
    assert "fundamentals" not in graph.selected_analysts


def test_no_unpassed_create_initial_state_params():
    """Fail loudly when upstream adds a state field the API path doesn't set.

    ``past_context`` is knowingly excluded: the API path never reads the
    per-user memory log back into a run, unlike propagate(). That's an open
    product question, not a settled decision — see the note in graph_factory.
    Anything else new should be wired up or explicitly skipped here.
    """
    params = set(inspect.signature(Propagator.create_initial_state).parameters)
    params -= {"self", "company_name", "trade_date"}
    assert params == {"asset_type", "past_context", "instrument_context"}, (
        f"create_initial_state signature changed: {sorted(params)} — "
        "check whether graph_factory needs to pass a new field"
    )
