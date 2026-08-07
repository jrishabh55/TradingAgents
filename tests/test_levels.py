"""Tests for the computed risk levels (apps/api/integrations/levels.py).

The market data is faked so the arithmetic is checked against numbers worked
out by hand — these tests must fail if a formula changes, not just if yfinance
is down.
"""
import math
from unittest.mock import patch

import pandas as pd
import pytest

from apps.api.integrations.levels import (
    LevelsUnavailable,
    compute_levels,
    parse_model_levels,
)


def _frame(closes, *, low_offset=2.0, high_offset=2.0):
    """OHLCV frame in the shape load_ohlcv returns."""
    return pd.DataFrame(
        {
            "Date": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
            "Close": closes,
            "High": [c + high_offset for c in closes],
            "Low": [c - low_offset for c in closes],
            "Open": closes,
            "Volume": [1_000_000] * len(closes),
        }
    )


def _levels(frame, *, atr=10.0, capital=1_000_000.0, risk_pct=1.0, r_multiple=2.0,
            rating="Buy", trader_plan=None, currency="USD"):
    """Run compute_levels with the market inputs pinned."""
    with patch("apps.api.integrations.levels.load_ohlcv", return_value=frame), \
         patch("apps.api.integrations.levels.wrap") as mock_wrap, \
         patch("apps.api.integrations.levels._quote_currency", return_value=currency):
        mock_wrap.return_value = {"atr": pd.Series([atr] * len(frame))}
        return compute_levels(
            run_id="run-1",
            ticker="TEST",
            analysis_date="2026-07-31",
            capital=capital,
            risk_pct=risk_pct,
            r_multiple=r_multiple,
            rating=rating,
            trader_plan=trader_plan,
        )


# A flat series: every close 100, lows 98, highs 102. Swing low = 98.
# With atr=10: structure stop = 98 - 0.25*10 = 95.5, distance 4.5 (0.45x ATR,
# inside the 3x cap) → risk/share 4.5.
FLAT = [100.0] * 60


def test_structure_stop_sits_below_the_swing_low_by_an_atr_buffer():
    r = _levels(_frame(FLAT))
    assert r.levels.stop.price == 95.5
    assert r.levels.entry.price == 100.0
    assert r.levels.risk_per_share == 4.5
    assert "swing low" in r.levels.stop.basis


def test_targets_are_multiples_of_the_risk_distance():
    r = _levels(_frame(FLAT), r_multiple=2.0)
    # 100 + 2*4.5 = 109, and the alt target is one R further.
    assert r.levels.target.price == 109.0
    assert r.levels.target_alt.price == 113.5


def test_position_size_is_derived_from_the_stop_distance():
    # 1% of 1,000,000 = 10,000 risk budget; 10,000 / 4.5 = 2222.2 → floor 2222.
    r = _levels(_frame(FLAT), capital=1_000_000.0, risk_pct=1.0)
    assert r.size.shares == 2222
    assert r.size.cash_risk == pytest.approx(9999.0, abs=0.01)
    assert r.size.position_value == pytest.approx(222_200.0, abs=0.01)


def test_halving_risk_pct_halves_the_size():
    full = _levels(_frame(FLAT), risk_pct=2.0).size.shares
    half = _levels(_frame(FLAT), risk_pct=1.0).size.shares
    assert half == full // 2


def test_wide_structure_falls_back_to_an_atr_stop():
    """A crash leaves the swing low far below entry — size off ATR instead."""
    # Recent lows are ~50 below the last close: 0.25 ATR buffer would put the
    # stop >3x ATR away, so the volatility rule takes over.
    closes = [150.0] * 10 + [100.0] * 50
    frame = _frame(closes, low_offset=2.0)
    frame.loc[frame.index[:10], "Low"] = 45.0  # deep low inside the lookback
    frame.loc[frame.index[10:], "Low"] = 48.0
    r = _levels(frame, atr=10.0)
    assert r.levels.stop.price == 80.0  # 100 - 2*10
    assert "ATR" in r.levels.stop.basis
    assert "too far to size off" in r.levels.stop.basis


def test_atr_fallback_does_not_by_itself_veto_the_trade():
    """An ATR stop is a valid method — only reward:risk and sizing gate viability.

    Regression: `viable` was computed from any note at all, so a legitimate
    volatility stop marked otherwise-clean setups (MSFT, KO) unviable.
    """
    closes = [150.0] * 10 + [100.0] * 50
    frame = _frame(closes, high_offset=80.0)  # nothing obstructing a 2R target
    frame.loc[frame.index[:10], "Low"] = 45.0
    frame.loc[frame.index[10:], "Low"] = 48.0
    r = _levels(frame, atr=10.0, r_multiple=2.0)
    assert "ATR" in r.levels.stop.basis  # the fallback did fire
    assert r.viability_notes == []
    assert r.viable is True


def test_target_above_resistance_reports_the_honest_reward_risk():
    """Reward:risk is measured to the level in the way, not past it."""
    # One genuine swing high at 107 — above the 0.5xATR noise floor (105) and
    # below the 2R target (109). R:R to it is (107-100)/4.5 = 1.56.
    frame = _frame(FLAT, high_offset=2.0)
    frame.loc[frame.index[30], "High"] = 107.0
    r = _levels(frame, r_multiple=2.0)
    assert r.levels.resistance.price == 107.0
    assert r.levels.reward_risk_ratio == pytest.approx(1.56, abs=0.01)
    assert r.viable is False
    assert any("resistance" in n for n in r.viability_notes)
    assert any("skip this trade" in n for n in r.viability_notes)


def test_a_wick_just_above_entry_is_not_treated_as_resistance():
    """Regression from real AAPL data.

    A bar poking 0.5 above entry was registering as resistance and dragging
    the measured reward:risk to 0.04, marking almost every setup unviable.
    Only swing highs beyond the ATR noise floor count.
    """
    frame = _frame(FLAT, high_offset=2.0)
    frame.loc[frame.index[30], "High"] = 100.5  # entry is 100.0, ATR 10
    r = _levels(frame, atr=10.0, r_multiple=2.0)
    assert r.levels.resistance is None
    assert r.levels.reward_risk_ratio == 2.0
    assert r.viable is True


def test_the_entry_bar_cannot_be_its_own_resistance():
    """The final bars can't form a centered pivot, so entry never blocks itself."""
    frame = _frame(FLAT, high_offset=2.0)
    frame.loc[frame.index[-1], "High"] = 130.0  # a spike on the entry bar
    r = _levels(frame, atr=10.0, r_multiple=2.0)
    assert r.levels.resistance is None


def test_clean_setup_is_viable_with_no_notes():
    # Highs far above so nothing obstructs a 2R target.
    r = _levels(_frame(FLAT, high_offset=80.0), r_multiple=2.0)
    assert r.viable is True
    assert r.viability_notes == []
    assert r.levels.reward_risk_ratio == 2.0


def test_reward_risk_below_minimum_is_not_viable():
    r = _levels(_frame(FLAT, high_offset=80.0), r_multiple=1.5)
    assert r.viable is False
    assert any("below the 2:1 minimum" in n for n in r.viability_notes)


@pytest.mark.parametrize("rating", ["Sell", "Underweight", "sell"])
def test_short_ratings_return_no_levels(rating):
    """Long-only in v1 — better to say so than emit long-shaped numbers."""
    r = _levels(_frame(FLAT), rating=rating)
    assert r.viable is False
    assert r.levels is None
    assert r.size is None
    assert any("short side is not supported" in n for n in r.viability_notes)


def test_hold_rating_shows_levels_but_is_not_viable():
    r = _levels(_frame(FLAT, high_offset=80.0), rating="Hold")
    assert r.levels is not None  # reference only
    assert r.viable is False
    assert any("no position is implied" in n for n in r.viability_notes)


def test_capital_too_small_for_one_share():
    # 1% of 100 = 1.00 risk budget vs 4.50 risk per share.
    r = _levels(_frame(FLAT, high_offset=80.0), capital=100.0, risk_pct=1.0)
    assert r.size.shares == 0
    assert r.size.cash_risk == 0
    assert r.viable is False
    assert any("no position fits" in n for n in r.viability_notes)


def test_currency_is_reported_and_capital_is_not_converted():
    r = _levels(_frame(FLAT), currency="INR", capital=1_000_000.0)
    assert r.currency == "INR"
    assert r.size.capital == 1_000_000.0  # echoed as given, never FX-adjusted


def test_missing_atr_is_an_explicit_failure_not_a_guess():
    with patch("apps.api.integrations.levels.load_ohlcv", return_value=_frame(FLAT)), \
         patch("apps.api.integrations.levels.wrap") as mock_wrap:
        mock_wrap.return_value = {"atr": pd.Series([float("nan")] * len(FLAT))}
        with pytest.raises(LevelsUnavailable, match="ATR unavailable"):
            compute_levels(
                run_id="r", ticker="TEST", analysis_date="2026-07-31",
                capital=1000.0, risk_pct=1.0, r_multiple=2.0, rating="Buy",
            )


def test_empty_market_data_is_an_explicit_failure():
    with patch("apps.api.integrations.levels.load_ohlcv", return_value=pd.DataFrame()):
        with pytest.raises(LevelsUnavailable, match="no market data rows"):
            compute_levels(
                run_id="r", ticker="TEST", analysis_date="2026-07-31",
                capital=1000.0, risk_pct=1.0, r_multiple=2.0, rating="Buy",
            )


def test_vendor_error_becomes_levels_unavailable():
    with patch("apps.api.integrations.levels.load_ohlcv", side_effect=RuntimeError("boom")):
        with pytest.raises(LevelsUnavailable, match="no usable market data"):
            compute_levels(
                run_id="r", ticker="TEST", analysis_date="2026-07-31",
                capital=1000.0, risk_pct=1.0, r_multiple=2.0, rating="Buy",
            )


# --- the agent's asserted levels, parsed for comparison only ---------------


def test_parses_the_traders_rendered_levels():
    plan = (
        "**Action**: Buy\n\n**Reasoning**: momentum\n\n"
        "**Entry Price**: 101.5\n\n**Stop Loss**: 88.0\n\n"
        "**Position Sizing**: 5% of portfolio\n"
    )
    m = parse_model_levels(plan)
    assert m.entry_price == 101.5
    assert m.stop_loss == 88.0
    assert m.position_sizing == "5% of portfolio"


def test_missing_agent_levels_parse_to_none():
    m = parse_model_levels("**Action**: Hold\n\n**Reasoning**: unclear\n")
    assert m.stop_loss is None and m.entry_price is None


def test_divergence_is_reported_against_the_computed_stop():
    plan = "**Stop Loss**: 88.0\n"
    r = _levels(_frame(FLAT, high_offset=80.0), trader_plan=plan)
    assert r.model_suggested.stop_loss == 88.0
    assert r.divergence is not None
    assert "below the computed stop" in r.divergence
    # The computed number must not be influenced by the agent's.
    assert r.levels.stop.price == 95.5


def test_agreeing_stops_are_called_out_as_agreeing():
    r = _levels(_frame(FLAT, high_offset=80.0), trader_plan="**Stop Loss**: 95.6\n")
    assert "within 1%" in r.divergence


def test_every_number_carries_a_basis():
    r = _levels(_frame(FLAT, high_offset=80.0))
    for value in (r.levels.entry, r.levels.stop, r.levels.target, r.levels.target_alt):
        assert value.basis and not value.basis.isspace()
    assert r.size.basis
    assert "not a price prediction" in r.disclaimer


def test_risk_pct_of_entry_is_reported():
    r = _levels(_frame(FLAT, high_offset=80.0))
    assert r.levels.risk_pct_of_entry == 4.5  # 4.5 / 100 * 100
    assert math.isclose(
        r.levels.risk_per_share / r.levels.entry.price * 100,
        r.levels.risk_pct_of_entry,
        abs_tol=0.01,
    )


# --- endpoint wiring: GET /api/runs/{id}/levels -----------------------------


@pytest.fixture
def client(monkeypatch):
    """App in open-auth mode (anonymous user) so ownership checks pass.

    ``create_app`` calls ``load_dotenv``, which would pull the developer's real
    ``.env`` — including live Clerk vars — back into the environment and make
    every request 401. Stub it out, then clear the vars.
    """
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from apps.api.auth import reset_verifier_for_tests

    monkeypatch.setattr("apps.api.app.load_dotenv", lambda *a, **k: None)
    for var in ("CLERK_JWKS_URL", "CLERK_ISSUER", "WEBAPP_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reset_verifier_for_tests()
    yield TestClient(create_app())
    reset_verifier_for_tests()


def _stub_run(status="completed", user_id=None, rating="Buy"):
    from datetime import datetime

    from apps.api.schemas import RunDetail

    return RunDetail(
        id="run-1",
        ticker="AAPL",
        analysis_date="2026-07-31",
        status=status,
        created_at=datetime(2026, 7, 31, 12, 0, 0),
        rating=rating,
        user_id=user_id,
        config={},
        trader_investment_plan="**Stop Loss**: 285.0\n",
    )


def _with_run(monkeypatch, detail):
    """Point the route's store at a stub returning ``detail``."""
    class _Store:
        def get_run(self, run_id):
            return detail

    monkeypatch.setattr("apps.api.routes.runs.get_store", lambda: _Store())


def test_endpoint_returns_computed_levels(client, monkeypatch):
    _with_run(monkeypatch, _stub_run())
    with patch("apps.api.integrations.levels.load_ohlcv", return_value=_frame(FLAT, high_offset=80.0)), \
         patch("apps.api.integrations.levels.wrap", return_value={"atr": pd.Series([10.0] * 60)}), \
         patch("apps.api.integrations.levels._quote_currency", return_value="USD"):
        r = client.get("/api/runs/run-1/levels", params={"capital": 1_000_000})
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "USD"
    assert body["levels"]["stop"]["price"] == 95.5
    assert body["size"]["shares"] == 2222
    # The agent's asserted stop is echoed for comparison but not used.
    assert body["model_suggested"]["stop_loss"] == 285.0
    assert "not a price prediction" in body["disclaimer"]


def test_endpoint_404s_for_unknown_run(client, monkeypatch):
    _with_run(monkeypatch, None)
    r = client.get("/api/runs/nope/levels", params={"capital": 1000})
    assert r.status_code == 404


def test_endpoint_404s_for_another_users_run(client, monkeypatch):
    """404 not 403, so it doesn't confirm the run exists for someone else."""
    _with_run(monkeypatch, _stub_run(user_id="user_other"))
    r = client.get("/api/runs/run-1/levels", params={"capital": 1000})
    assert r.status_code == 404


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_endpoint_409s_until_the_run_completes(client, monkeypatch, status):
    _with_run(monkeypatch, _stub_run(status=status))
    r = client.get("/api/runs/run-1/levels", params={"capital": 1000})
    assert r.status_code == 409


def test_endpoint_503s_when_market_data_is_unusable(client, monkeypatch):
    _with_run(monkeypatch, _stub_run())
    with patch(
        "apps.api.integrations.levels.load_ohlcv", side_effect=RuntimeError("vendor down")
    ):
        r = client.get("/api/runs/run-1/levels", params={"capital": 1000})
    assert r.status_code == 503
    assert "no usable market data" in r.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {},                                        # capital is required
        {"capital": 0},                            # must be > 0
        {"capital": -5},
        {"capital": 1000, "risk_pct": 0},          # must be > 0
        {"capital": 1000, "risk_pct": 101},        # must be <= 100
        {"capital": 1000, "r_multiple": 0.5},      # must be >= 1
        {"capital": 1000, "r_multiple": 99},       # must be <= 10
    ],
)
def test_endpoint_rejects_bad_parameters(client, monkeypatch, params):
    _with_run(monkeypatch, _stub_run())
    r = client.get("/api/runs/run-1/levels", params=params)
    assert r.status_code == 422
