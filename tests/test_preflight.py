"""Tests for the two fail-fast gates on missing market data.

Gate A — ``preflight_ticker`` at submit time (apps/api/integrations/preflight.py)
Gate B — ``find_price_data_failure`` per chunk (apps/api/jobs/runner.py)

Both exist because a run for ``NSEI`` (Nifty 50 is ``^NSEI``) completed the full
pipeline with no prices and emitted "Rating: Hold" — which is also the parser's
default, so it was indistinguishable from no answer at all.
"""
from unittest.mock import patch

import pandas as pd
import pytest

from apps.api.integrations.preflight import (
    PreflightResult,
    _candidates,
    preflight_ticker,
)
from apps.api.jobs.runner import find_price_data_failure


def _ok_frame():
    return pd.DataFrame(
        {
            "Date": pd.date_range("2026-01-01", periods=5, freq="D"),
            "Close": [1.0] * 5,
            "High": [1.0] * 5,
            "Low": [1.0] * 5,
            "Open": [1.0] * 5,
            "Volume": [1] * 5,
        }
    )


def _resolver(*resolving):
    """Fake load_ohlcv where only ``resolving`` symbols return rows."""
    allowed = set(resolving)

    def _load(symbol, _date):
        if symbol in allowed:
            return _ok_frame()
        raise RuntimeError(f"no data for {symbol}")

    return _load


# ---------- Gate A: preflight ----------


def test_valid_ticker_passes_without_probing():
    load = _resolver("RELIANCE.NS")
    with patch("apps.api.integrations.preflight.load_ohlcv", side_effect=load) as m:
        r = preflight_ticker("RELIANCE.NS", "2026-08-01")
    assert r.ok is True
    assert r.suggestions == []
    # Exactly one call — a good ticker must not pay for suggestion probes.
    assert m.call_count == 1


def test_bare_index_suggests_the_caret_form():
    """The real bug: NSEI has no data, ^NSEI does."""
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^NSEI")
    ):
        r = preflight_ticker("NSEI", "2026-08-01")
    assert r.ok is False
    assert r.suggestions == ["^NSEI"]
    assert "Did you mean: ^NSEI?" in r.as_error_detail()


def test_alias_is_offered_for_names_that_are_not_mechanical():
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^BSESN")
    ):
        r = preflight_ticker("SENSEX", "2026-08-01")
    assert r.suggestions == ["^BSESN"]


def test_bare_equity_suggests_the_nse_suffix():
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("RELIANCE.NS")
    ):
        r = preflight_ticker("RELIANCE", "2026-08-01")
    assert r.suggestions == ["RELIANCE.NS"]


def test_unknown_symbol_offers_nothing_rather_than_guessing():
    """No suggestion is better than a wrong one."""
    with patch("apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver()):
        r = preflight_ticker("ZZZZFAKE", "2026-08-01")
    assert r.ok is False
    assert r.suggestions == []
    assert "Did you mean" not in r.as_error_detail()


def test_empty_frame_counts_as_no_data():
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", return_value=pd.DataFrame()
    ):
        assert preflight_ticker("AAPL", "2026-08-01").ok is False


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("NSEI", ["^NSEI", "NSEI.NS"]),
        ("NIFTY", ["^NSEI", "^NIFTY", "NIFTY.NS"]),   # alias first
        ("^NSEI", []),          # already an index — nothing to probe
        ("RELIANCE.NS", []),    # already suffixed
        ("BTC-USD", []),        # crypto pair
        ("GC=F", []),           # futures
    ],
)
def test_candidate_probes_respect_explicit_conventions(ticker, expected):
    """Never probe variants of a symbol that already states its convention."""
    assert _candidates(ticker) == expected


def test_preflight_never_raises_on_vendor_explosion():
    """A preflight failure must read as a 400, never a 500."""
    with patch(
        "apps.api.integrations.preflight.load_ohlcv",
        side_effect=MemoryError("vendor exploded"),
    ):
        r = preflight_ticker("AAPL", "2026-08-01")
    assert isinstance(r, PreflightResult) and r.ok is False


# ---------- Gate B: mid-run abort ----------


class _Msg:
    """Minimal stand-in for a LangChain ToolMessage."""

    def __init__(self, name, content):
        self.name = name
        self.content = content


NO_DATA = (
    "NO_DATA_AVAILABLE: No usable market data for 'NSEI' from any configured "
    "vendor. The symbol may be invalid, delisted, not covered..."
)


def test_price_tool_no_data_trips_the_gate():
    chunk = {"messages": [_Msg("get_stock_data", NO_DATA)]}
    reason = find_price_data_failure(chunk)
    assert reason is not None
    assert "get_stock_data" in reason
    assert "NSEI" in reason


@pytest.mark.parametrize(
    "tool", ["get_stock_data", "get_indicators", "get_verified_market_snapshot"]
)
def test_every_price_tool_is_watched(tool):
    assert find_price_data_failure({"messages": [_Msg(tool, NO_DATA)]}) is not None


@pytest.mark.parametrize(
    "tool", ["get_fundamentals", "get_balance_sheet", "get_news", "get_macro_indicators"]
)
def test_non_price_tools_do_not_trip_the_gate(tool):
    """An index has no balance sheet — gating on fundamentals would reject it."""
    assert find_price_data_failure({"messages": [_Msg(tool, NO_DATA)]}) is None


def test_healthy_chunk_does_not_trip():
    chunk = {"messages": [_Msg("get_stock_data", "Date,Close\n2026-01-01,100\n")]}
    assert find_price_data_failure(chunk) is None


def test_chunk_without_messages_is_safe():
    assert find_price_data_failure({}) is None
    assert find_price_data_failure({"messages": None}) is None
    assert find_price_data_failure({"market_report": "text"}) is None


def test_non_string_tool_content_is_ignored():
    """Structured tool payloads must not crash the gate."""
    assert find_price_data_failure({"messages": [_Msg("get_stock_data", {"a": 1})]}) is None
    assert find_price_data_failure({"messages": [_Msg("get_stock_data", None)]}) is None


def test_sentinel_must_be_the_tool_result_not_prose_about_it():
    """The analyst's report is LLM prose in the run's output_language.

    Matching report text would break for a non-English run, so only a tool
    message whose content *starts with* the sentinel counts.
    """
    quoted = "The tool returned NO_DATA_AVAILABLE, so I cannot analyse this."
    assert find_price_data_failure({"messages": [_Msg("get_stock_data", quoted)]}) is None


def test_message_without_a_name_is_ignored():
    assert find_price_data_failure({"messages": [_Msg(None, NO_DATA)]}) is None


# ---------- Gate A over HTTP: POST /api/runs ----------


@pytest.fixture
def client(monkeypatch):
    """App in open-auth mode. ``create_app`` calls ``load_dotenv``, which would
    pull the developer's real Clerk vars back in and 401 every request."""
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from apps.api.auth import reset_verifier_for_tests

    monkeypatch.setattr("apps.api.app.load_dotenv", lambda *a, **k: None)
    for var in ("CLERK_JWKS_URL", "CLERK_ISSUER", "WEBAPP_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reset_verifier_for_tests()
    yield TestClient(create_app())
    reset_verifier_for_tests()


def _payload(ticker="NSEI"):
    return {
        "ticker": ticker,
        "analysis_date": "2026-08-01",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "openai",
        "shallow_thinker": "gpt-5.4-mini",
        "deep_thinker": "gpt-5.4",
        "backend_url": "https://api.openai.com/v1",
    }


@pytest.fixture
def wired(monkeypatch):
    """Stub store + runner; returns the list of tickers actually enqueued."""
    from datetime import datetime

    from apps.api.schemas import RunDetail

    enqueued = []

    class _Store:
        def find_cached_run(self, *_a, **_k):
            return None

        def has_active_run_for_ticker(self, *_a, **_k):
            return False

        def create_run(self, **kw):
            enqueued.append(kw["ticker"])
            return "run-1"

        def get_run(self, _run_id):
            return RunDetail(
                id="run-1", ticker="NSEI", analysis_date="2026-08-01",
                status="queued", created_at=datetime(2026, 8, 1), config={},
            )

    class _Runner:
        def submit(self, *_a, **_k):
            return None

    monkeypatch.setattr("apps.api.routes.runs.get_store", lambda: _Store())
    monkeypatch.setattr("apps.api.routes.runs.get_runner", lambda: _Runner())
    return enqueued


def test_post_rejects_a_ticker_with_no_prices(client, wired):
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^NSEI")
    ):
        r = client.post("/api/runs", json=_payload("NSEI"))
    assert r.status_code == 400
    # Structured, so the UI can render one-tap fixes rather than regex a sentence.
    detail = r.json()["detail"]
    assert detail["code"] == "no_market_data"
    assert detail["ticker"] == "NSEI"
    assert detail["suggestions"] == ["^NSEI"]
    assert "^NSEI" in detail["message"]
    assert wired == []  # nothing enqueued — the whole point


def test_post_accepts_a_resolvable_ticker(client, wired):
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^NSEI")
    ):
        r = client.post("/api/runs", json=_payload("^NSEI"))
    assert r.status_code == 201
    assert wired == ["^NSEI"]


def test_skip_preflight_overrides_the_gate(client, wired):
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^NSEI")
    ):
        r = client.post(
            "/api/runs", json=_payload("NSEI"), params={"skip_preflight": "true"}
        )
    assert r.status_code == 201
    assert wired == ["NSEI"]


def test_force_does_not_disable_preflight(client, wired):
    """`force` means "bypass the cache" — it must not also skip validation."""
    with patch(
        "apps.api.integrations.preflight.load_ohlcv", side_effect=_resolver("^NSEI")
    ):
        r = client.post("/api/runs", json=_payload("NSEI"), params={"force": "true"})
    assert r.status_code == 400
    assert wired == []
