"""Deterministic risk levels for a completed run — computed, not model-generated.

The Trader agent emits ``entry_price`` / ``stop_loss`` / ``position_sizing`` as
free-form structured fields, but it runs with NO_EXTERNAL_TOOLS and only ever
sees the Research Manager's prose — it never sees a price. So those numbers are
asserted, not derived.

This module derives them instead, from the same look-ahead-filtered OHLCV the
market analyst uses, with one strategy:

    structure-based stop + risk-multiple target + fixed-fractional sizing

Chosen because it's the combination that ties every number to something
observable: the stop to a chart level, the target to the stop distance, the
size to the stop distance and a fixed risk budget. Nothing here predicts price.

Read-only imports from the upstream core (``load_ohlcv``, ``stockstats``) — no
core edits, so this survives upstream pulls untouched.

ponytail: one strategy, not a registry. Percentage / MA / time stops, Kelly,
Fibonacci and measured-move targets, trailing exits and scale-outs are all
deliberately absent — add one when a real trading style needs it, not before.
"""
from __future__ import annotations

import functools
import math
import re
from typing import Optional, Tuple

from stockstats import wrap

from apps.api.schemas import (
    ComputedLevels,
    LevelsResponse,
    LevelValue,
    ModelSuggested,
    PositionSize,
)
from tradingagents.dataflows.stockstats_utils import load_ohlcv

# Bars of history used to find the swing low the stop sits under. ~1 month of
# trading days: recent enough to be the level that invalidates a current
# thesis, long enough not to be a single day's noise.
SWING_LOOKBACK = 20

# Bars searched for the nearest resistance above entry. ~3 months, so a target
# is checked against levels a trader would actually be watching.
RESISTANCE_LOOKBACK = 60

# Half-width of the window a bar must dominate to count as a swing high. A
# plain "highest high above entry" is not usable here: any single wick a few
# cents above the current price would register as resistance and drag the
# measured reward:risk toward zero. A pivot has to be the highest bar within
# +/- this many bars on both sides.
PIVOT_WINDOW = 3

# A resistance level closer than this many ATRs to entry is noise, not a
# barrier worth measuring a target against.
MIN_RESISTANCE_ATR = 0.5

# Cushion below the swing low, in ATR units. A stop exactly at the low gets
# taken out by noise that doesn't invalidate anything.
STOP_BUFFER_ATR = 0.25

# If a structure stop is further than this many ATRs, the structure is broken
# (gap-down, vertical drop) and sizing off it would be absurdly small — fall
# back to a pure volatility stop.
MAX_STRUCTURE_ATR = 3.0
ATR_STOP_MULTIPLE = 2.0

# Minimum reward:risk to call a trade viable. Below this the rules say skip it.
MIN_REWARD_RISK = 2.0

# Ratings that imply a short or no position. Long-only in v1.
_SHORT_RATINGS = {"underweight", "sell"}
_FLAT_RATINGS = {"hold"}

# The Trader's rendered markdown, e.g. "**Stop Loss**: 178.0".
_MODEL_LEVEL_RE = {
    "stop_loss": re.compile(r"\*\*Stop Loss\*\*:\s*([0-9.]+)", re.IGNORECASE),
    "entry_price": re.compile(r"\*\*Entry Price\*\*:\s*([0-9.]+)", re.IGNORECASE),
}
_MODEL_SIZING_RE = re.compile(r"\*\*Position Sizing\*\*:\s*(.+)", re.IGNORECASE)


class LevelsUnavailable(RuntimeError):
    """Raised when OHLCV for the ticker/date can't support a calculation."""


@functools.lru_cache(maxsize=512)
def _quote_currency(ticker: str) -> Optional[str]:
    """The instrument's quote currency, or None if unresolvable.

    Fail-open and cached: a missing currency label degrades the response to
    "unknown currency", it never blocks the calculation. Capital is assumed to
    already be in this currency — no FX conversion happens anywhere here.
    """
    try:
        import yfinance as yf

        from tradingagents.dataflows.symbol_utils import normalize_symbol

        info = yf.Ticker(normalize_symbol(ticker)).info or {}
        currency = info.get("currency")
        return str(currency).upper() if currency else None
    except Exception:  # noqa: BLE001 — a label is not worth failing a request over
        return None


def _round(value: float) -> float:
    return round(float(value), 2)


def parse_model_levels(trader_plan: Optional[str]) -> ModelSuggested:
    """Pull the Trader's asserted levels out of its rendered markdown.

    Comparison only — these never feed the computed numbers.
    """
    if not trader_plan:
        return ModelSuggested()
    found: dict = {}
    for field, pattern in _MODEL_LEVEL_RE.items():
        m = pattern.search(trader_plan)
        if m:
            try:
                found[field] = float(m.group(1))
            except ValueError:
                pass
    m = _MODEL_SIZING_RE.search(trader_plan)
    if m:
        found["position_sizing"] = m.group(1).strip()
    return ModelSuggested(**found)


def _describe_divergence(computed_stop: float, model_stop: Optional[float]) -> Optional[str]:
    """One sentence on how far the agent's stop sits from the computed one."""
    if model_stop is None or computed_stop <= 0:
        return None
    delta_pct = (model_stop - computed_stop) / computed_stop * 100
    if abs(delta_pct) < 1:
        return "Agent stop is within 1% of the computed structure stop."
    direction = "above" if delta_pct > 0 else "below"
    return (
        f"Agent stop ({model_stop:.2f}) sits {abs(delta_pct):.1f}% {direction} the "
        f"computed stop ({computed_stop:.2f}). The computed level is derived from "
        "price structure; the agent's was asserted without price access."
    )


def _market_inputs(ticker: str, analysis_date: str) -> Tuple[float, float, float, Optional[float]]:
    """(entry, atr, swing_low, resistance) from look-ahead-filtered OHLCV."""
    try:
        df = load_ohlcv(ticker, analysis_date)
    except Exception as exc:  # noqa: BLE001 — vendor errors become a 503 upstream
        raise LevelsUnavailable(f"no usable market data for {ticker}: {exc}") from exc
    if df is None or df.empty:
        raise LevelsUnavailable(f"no market data rows for {ticker} on or before {analysis_date}")

    entry = float(df.iloc[-1]["Close"])
    if not math.isfinite(entry) or entry <= 0:
        raise LevelsUnavailable(f"last close for {ticker} is not a usable price: {entry!r}")

    stock_df = wrap(df.copy())
    try:
        atr = float(stock_df["atr"].iloc[-1])
    except Exception:  # noqa: BLE001 — too little history for ATR
        atr = float("nan")
    if not math.isfinite(atr) or atr <= 0:
        raise LevelsUnavailable(
            f"ATR unavailable for {ticker} (needs more history than {len(df)} bars)"
        )

    swing_low = float(df["Low"].tail(SWING_LOOKBACK).min())
    resistance = _nearest_resistance(df, entry, atr)

    return entry, atr, swing_low, resistance


def _nearest_resistance(df, entry: float, atr: float) -> Optional[float]:
    """Nearest swing high meaningfully above entry, or None if unobstructed.

    A swing high is a bar whose High dominates the ``PIVOT_WINDOW`` bars on
    either side, so a lone wick just above the current price doesn't count as a
    barrier. Levels within ``MIN_RESISTANCE_ATR`` of entry are skipped for the
    same reason — measuring reward against noise makes every setup look
    unviable.
    """
    highs = df["High"].tail(RESISTANCE_LOOKBACK).reset_index(drop=True)
    span = 2 * PIVOT_WINDOW + 1
    if len(highs) < span:
        return None

    # A centered rolling max leaves NaN in the first/last PIVOT_WINDOW slots,
    # which conveniently excludes the entry bar itself from being a pivot.
    centered_max = highs.rolling(span, center=True, min_periods=span).max()
    floor_price = entry + MIN_RESISTANCE_ATR * atr

    candidates = [
        float(high)
        for high, local_max in zip(highs, centered_max)
        if math.isfinite(float(high))
        and math.isfinite(float(local_max))
        and float(high) >= float(local_max)
        and float(high) > floor_price
    ]
    return min(candidates) if candidates else None


def compute_levels(
    *,
    run_id: str,
    ticker: str,
    analysis_date: str,
    capital: float,
    risk_pct: float,
    r_multiple: float,
    rating: Optional[str] = None,
    trader_plan: Optional[str] = None,
) -> LevelsResponse:
    """Derive stop, target and position size for a long entry.

    Raises ``LevelsUnavailable`` when the market data can't support the
    calculation. Never raises for a merely *unattractive* setup — that comes
    back as ``viable=false`` with the reasons, because "skip this trade" is a
    real answer and the caller should see the numbers behind it.

    ``viability_notes`` holds only reasons NOT to take the trade. How a level
    was derived belongs in that level's ``basis``, never here — otherwise a
    valid-but-unusual stop rule silently vetoes an attractive setup.
    """
    model_suggested = parse_model_levels(trader_plan)
    notes: list[str] = []

    normalized_rating = (rating or "").strip().lower()
    if normalized_rating in _SHORT_RATINGS:
        # Return early and empty rather than long-shaped numbers for a short.
        return LevelsResponse(
            run_id=run_id,
            ticker=ticker,
            analysis_date=analysis_date,
            rating=rating,
            currency=_quote_currency(ticker),
            viable=False,
            viability_notes=[
                f"Rating is {rating} — the short side is not supported yet, so no "
                "levels are computed (long-only arithmetic would be misleading)."
            ],
            model_suggested=model_suggested,
        )

    entry, atr, swing_low, resistance = _market_inputs(ticker, analysis_date)

    # --- stop: structure first, volatility as the fallback -----------------
    structure_stop = swing_low - STOP_BUFFER_ATR * atr
    structure_distance = entry - structure_stop
    if structure_distance > MAX_STRUCTURE_ATR * atr or structure_distance <= 0:
        stop_price = entry - ATR_STOP_MULTIPLE * atr
        stop_basis = (
            f"{ATR_STOP_MULTIPLE:g}x ATR below entry (ATR {atr:.2f}); the "
            f"{SWING_LOOKBACK}-bar swing low at {swing_low:.2f} was "
            f"{structure_distance / atr:.1f}x ATR away — too far to size off"
        )
        # Deliberately NOT a viability note: an ATR stop is a legitimate method,
        # not a reason to skip the trade. `stop.basis` already states which rule
        # fired and why, so viability_notes stays strictly "reasons not to trade".
    else:
        stop_price = structure_stop
        stop_basis = (
            f"{SWING_LOOKBACK}-bar swing low {swing_low:.2f} less "
            f"{STOP_BUFFER_ATR:g}x ATR buffer ({atr:.2f})"
        )

    risk_per_share = entry - stop_price
    if risk_per_share <= 0:  # pragma: no cover — guarded by the ATR branch above
        raise LevelsUnavailable("computed stop is not below entry; cannot size a long")

    # --- targets: multiples of the risk distance ---------------------------
    primary = entry + r_multiple * risk_per_share
    alt_multiple = r_multiple + 1
    alt = entry + alt_multiple * risk_per_share

    reward_risk = r_multiple
    if resistance is not None and primary > resistance:
        # The honest reward:risk is measured to the level that's actually in
        # the way, not to an unobstructed multiple.
        reward_risk = (resistance - entry) / risk_per_share
        notes.append(
            f"{r_multiple:g}R target ({primary:.2f}) sits above the nearest "
            f"resistance ({resistance:.2f}); reward:risk to that level is only "
            f"{reward_risk:.2f}:1."
        )

    if reward_risk < MIN_REWARD_RISK:
        notes.append(
            f"Reward:risk of {reward_risk:.2f}:1 is below the {MIN_REWARD_RISK:g}:1 "
            "minimum — the rules say skip this trade."
        )

    # --- size: derived from the stop distance, never guessed ---------------
    risk_budget = capital * risk_pct / 100.0
    shares = int(math.floor(risk_budget / risk_per_share))
    if shares < 1:
        notes.append(
            f"Risk budget ({risk_budget:.2f}) is smaller than the risk on a single "
            f"share ({risk_per_share:.2f}) — no position fits these constraints."
        )
    if normalized_rating in _FLAT_RATINGS:
        notes.append(
            f"Rating is {rating} — no position is implied. Levels are shown for "
            "reference only."
        )

    return LevelsResponse(
        run_id=run_id,
        ticker=ticker,
        analysis_date=analysis_date,
        rating=rating,
        currency=_quote_currency(ticker),
        viable=not notes,
        viability_notes=notes,
        levels=ComputedLevels(
            entry=LevelValue(
                price=_round(entry),
                basis=f"last close on or before {analysis_date}",
            ),
            stop=LevelValue(price=_round(stop_price), basis=stop_basis),
            target=LevelValue(
                price=_round(primary),
                basis=f"{r_multiple:g}x risk ({risk_per_share:.2f}) above entry",
            ),
            target_alt=LevelValue(
                price=_round(alt),
                basis=f"{alt_multiple:g}x risk ({risk_per_share:.2f}) above entry",
            ),
            risk_per_share=_round(risk_per_share),
            risk_pct_of_entry=_round(risk_per_share / entry * 100),
            reward_risk_ratio=_round(reward_risk),
            resistance=(
                None
                if resistance is None
                else LevelValue(
                    price=_round(resistance),
                    basis=(
                        f"nearest swing high (dominates +/-{PIVOT_WINDOW} bars) above "
                        f"entry in the last {RESISTANCE_LOOKBACK} bars"
                    ),
                )
            ),
        ),
        size=PositionSize(
            shares=max(shares, 0),
            cash_risk=_round(max(shares, 0) * risk_per_share),
            position_value=_round(max(shares, 0) * entry),
            capital=_round(capital),
            risk_pct=risk_pct,
            basis=(
                f"floor(({capital:.2f} x {risk_pct:g}%) / {risk_per_share:.2f} risk "
                "per share) — stop distance sets the size"
            ),
        ),
        model_suggested=model_suggested,
        divergence=_describe_divergence(stop_price, model_suggested.stop_loss),
    )
