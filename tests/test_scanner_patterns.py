from __future__ import annotations

import pandas as pd
import pytest

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


def test_inverted_hammer():
    # Long upper shadow, small body at bottom, tiny lower shadow.
    p = panel_ohlc([(100, 102.5, 99.5, 100.5)])
    assert pattern_frame("inverted_hammer", p)["A"].iloc[-1]


def test_shooting_star():
    # Long upper shadow, small body, tiny lower shadow in uptrend.
    # Need 4 rows to establish uptrend: c.shift(1) > c.shift(3) at row 3.
    p = panel_ohlc([(99, 99.5, 98.5, 99.2),      # row 0: c=99.2
                    (99.3, 99.8, 99, 99.5),      # row 1: c=99.5
                    (99.6, 100.1, 99.3, 99.9),   # row 2: c=99.9 (uptrend: 99.9>99.2)
                    (100.5, 102.5, 99.5, 100)])  # row 3: inverted_hammer shape, red body
    assert pattern_frame("shooting_star", p)["A"].iloc[-1]


def test_hanging_man():
    # Long lower shadow, small body at top, tiny upper shadow in uptrend.
    # Need 4 rows to establish uptrend: c.shift(1) > c.shift(3) at row 3.
    p = panel_ohlc([(99, 99.5, 98.5, 99.2),      # row 0: c=99.2
                    (99.3, 99.8, 99, 99.5),      # row 1: c=99.5
                    (99.6, 100.1, 99.3, 99.9),   # row 2: c=99.9 (uptrend: 99.9>99.2)
                    (100, 100.6, 95, 100.5)])    # row 3: hammer shape
    assert pattern_frame("hanging_man", p)["A"].iloc[-1]


def test_bearish_engulfing():
    # Green bar followed by red bar that fully engulfs prior body.
    p = panel_ohlc([(99.8, 100.3, 99.5, 102.5),  # green: o1=99.8, c1=102.5
                    (103, 103.5, 99, 99.5)])      # red: o=103, c=99.5
    assert pattern_frame("bearish_engulfing", p)["A"].iloc[-1]
    assert not pattern_frame("bullish_engulfing", p)["A"].iloc[-1]


def test_morning_star():
    # Red bar, small body doji, green bar closing above midpoint in downtrend.
    # Need 5 rows to check downtrend: c.shift(1) < c.shift(3) at row 4.
    p = panel_ohlc([(105, 105.5, 104.5, 105),    # row 0: c=105 (high start)
                    (104.5, 105, 104, 104.2),    # row 1: c=104.2
                    (104.2, 104.5, 99.5, 100),   # row 2: red large body, mid2=102.1
                    (99.8, 100.2, 99.6, 100),    # row 3: small body (0.2)
                    (99.9, 102.5, 99.5, 102.5)]) # row 4: green, c>mid2, downtrend at bar-1
    assert pattern_frame("morning_star", p)["A"].iloc[-1]


def test_evening_star():
    # Green bar, small body doji, red bar closing below midpoint in uptrend.
    # Need 5 rows to check uptrend: c.shift(1) > c.shift(3) at row 4.
    p = panel_ohlc([(99, 99.5, 98.5, 99),        # row 0: c=99 (low start)
                    (99.2, 99.7, 99, 99.5),      # row 1: c=99.5
                    (99.5, 104.5, 99, 104),      # row 2: green large body, mid2=101.75
                    (103.8, 104.2, 103.6, 104),  # row 3: small body (0.2)
                    (104, 104.5, 99.5, 100)])    # row 4: red, c<mid2, uptrend at bar-1
    assert pattern_frame("evening_star", p)["A"].iloc[-1]


def test_three_black_crows():
    # Three consecutive red bars with falling closes (mirror of three_white_soldiers).
    p = panel_ohlc([(102, 102.5, 100.5, 100),   # red
                    (101, 101.5, 99.5, 99),      # red
                    (100, 100.5, 98.5, 98)])     # red
    assert pattern_frame("three_black_crows", p)["A"].iloc[-1]


def test_dark_cloud_cover():
    # Green bar followed by red bar opening above prior close,
    # closing below midpoint (mirror of piercing).
    p = panel_ohlc([(100, 100.5, 99.5, 104),    # green: o1=100, c1=104
                    (104.5, 105, 99.5, 101)])    # red: o=104.5, c=101
    assert pattern_frame("dark_cloud_cover", p)["A"].iloc[-1]


def test_unknown_pattern_raises():
    # Unknown pattern name should raise ValueError.
    p = panel_ohlc([(100, 101, 99, 100.05)])
    with pytest.raises(ValueError, match="unknown pattern"):
        pattern_frame("head_and_shoulders", p)
