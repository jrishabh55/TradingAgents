"""Reject a run before it costs anything when the ticker has no market data.

Motivation: a run for ``NSEI`` (the Nifty 50 index, whose Yahoo symbol is
``^NSEI``) completed the full 12-agent pipeline with zero price data and emitted
"Rating: Hold" — which is also ``parse_rating``'s default, so it was
indistinguishable from "the parser found nothing". The market analyst correctly
refused to fabricate; nothing downstream gated on that refusal.

Resolving the ticker once at submit time costs ~0.5s on the accept path and
*warms the OHLCV cache the market analyst then uses*, so a valid run is no
slower. An invalid one is rejected in ~2s instead of burning the pipeline.

Suggestions are deterministic probes, not fuzzy matching: try the caret form and
the NSE form, offer whichever actually resolves. No ticker database, no
edit-distance scoring.

ponytail: prices only. Fundamentals are legitimately absent for indices and
commodities (``^NSEI`` has OHLCV but no balance sheet), and news/sentiment
already degrade to sentinels upstream — gating on those would reject valid runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from tradingagents.dataflows.stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)


# Index names users type that aren't a mechanical transform of the real symbol.
# Kept deliberately tiny — mechanical cases are handled by the probes below.
_ALIASES = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "BANKNIFTY": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
}

# Characters that mean the user already expressed an explicit market convention
# (``^`` index, ``.`` exchange suffix, ``-`` crypto pair, ``=`` futures/forex).
# Probing variants of those would just add pointless network round-trips.
_EXPLICIT_MARKERS = ("^", ".", "-", "=")


@dataclass
class PreflightResult:
    """Outcome of resolving a ticker's price data."""

    ok: bool
    ticker: str
    detail: str = ""
    #: Symbols that were probed and DO resolve — offer these to the user.
    suggestions: List[str] = field(default_factory=list)

    def as_error_detail(self) -> str:
        """Human-readable message for the HTTP 400 body."""
        msg = (
            f"No market data for '{self.ticker}'. {self.detail} "
            "The run was not started, since every agent downstream would have "
            "had no prices to work from."
        )
        if self.suggestions:
            msg += f" Did you mean: {', '.join(self.suggestions)}?"
        return msg


def _resolves(symbol: str, analysis_date: str) -> bool:
    """Whether OHLCV exists for ``symbol`` on or before ``analysis_date``."""
    try:
        df = load_ohlcv(symbol, analysis_date)
    except Exception as exc:  # noqa: BLE001 — any vendor failure means "no"
        logger.debug("Preflight: %s did not resolve: %s", symbol, exc)
        return False
    return df is not None and not df.empty


def _candidates(ticker: str) -> List[str]:
    """Symbols worth probing when ``ticker`` itself has no data.

    At most two network calls: the alias (if we know one) plus the caret and
    NSE forms, skipped when the user already used an explicit convention.
    """
    bare = ticker.strip().upper()
    out: List[str] = []

    alias = _ALIASES.get(bare)
    if alias:
        out.append(alias)

    if not any(marker in bare for marker in _EXPLICIT_MARKERS):
        # Indices are the common miss: NSEI -> ^NSEI, GSPC -> ^GSPC.
        out.append(f"^{bare}")
        # An Indian equity typed without its exchange suffix.
        out.append(f"{bare}.NS")

    # Preserve order, drop dupes and the input itself.
    seen = {bare}
    unique = []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def preflight_ticker(ticker: str, analysis_date: str) -> PreflightResult:
    """Check the ticker has price data; suggest alternatives when it doesn't.

    Never raises — a preflight failure must not look like a server error. On the
    happy path this also populates the OHLCV cache, so the market analyst's
    first tool call is a cache hit.
    """
    if _resolves(ticker, analysis_date):
        return PreflightResult(ok=True, ticker=ticker)

    suggestions = [c for c in _candidates(ticker) if _resolves(c, analysis_date)]
    return PreflightResult(
        ok=False,
        ticker=ticker,
        detail=(
            "The symbol may be invalid, delisted, not covered by the configured "
            "vendor, or missing an exchange suffix."
        ),
        suggestions=suggestions,
    )
