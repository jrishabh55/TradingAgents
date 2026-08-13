from __future__ import annotations

import pandas as pd

from apps.api.scanner.indicators import Panel
from apps.api.scanner.patterns import pattern_frame


def panel_ohlc(rows: list[tuple[float, float, float, float]]) -> Panel:
    """rows = [(open, high, low, close), ...] for one symbol 'A'."""
    idx = pd.date_range("2026-01-05", periods=len(rows), freq="D")
    o, h, l, c = (pd.DataFrame({"A": [r[i] for r in rows]}, index=idx, dtype=float)
                  for i in range(4))
    return Panel(open=o, high=h, low=l, close=c, volume=c * 0 + 1000,
                 fundamentals=pd.DataFrame(index=["A"]), meta=pd.DataFrame(index=["A"]))


def test_doji():
    p = panel_ohlc([(100, 101, 99, 100.05)])
    assert pattern_frame("doji", p)["A"].iloc[-1]
    p2 = panel_ohlc([(100, 105, 99, 105)])
    assert not pattern_frame("doji", p2)["A"].iloc[-1]


def test_hammer():
    # Long lower shadow, small body at the top, tiny upper shadow.
    p = panel_ohlc([(100, 100.6, 95, 100.5)])
    assert pattern_frame("hammer", p)["A"].iloc[-1]


def test_bullish_engulfing():
    p = panel_ohlc([(102, 102.5, 99.5, 100),    # red
                    (99.8, 103, 99.5, 102.5)])  # green engulfing prior body
    assert pattern_frame("bullish_engulfing", p)["A"].iloc[-1]
    assert not pattern_frame("bearish_engulfing", p)["A"].iloc[-1]


def test_three_white_soldiers():
    p = panel_ohlc([(100, 102.2, 99.8, 102), (101, 104.2, 100.8, 104),
                    (103, 106.2, 102.8, 106)])
    assert pattern_frame("three_white_soldiers", p)["A"].iloc[-1]


def test_piercing():
    p = panel_ohlc([(104, 104.5, 99.5, 100),      # red
                    (99, 103.5, 98.8, 102.5)])    # opens below prior close, closes above midpoint
    assert pattern_frame("piercing", p)["A"].iloc[-1]
