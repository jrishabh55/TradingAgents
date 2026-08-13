from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from apps.api.scanner.indicators import Panel, describe, eval_operand
from apps.api.scanner.schema import Operand


def panel_from_closes(closes: dict[str, list[float]], volumes=None) -> Panel:
    idx = pd.date_range("2026-01-05", periods=len(next(iter(closes.values()))), freq="D")
    c = pd.DataFrame(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c)
    v = pd.DataFrame(volumes, index=idx, dtype=float) if volumes else c * 0 + 1000
    return Panel(open=o, high=np.maximum(o, c) * 1.001, low=np.minimum(o, c) * 0.999,
                 close=c, volume=v,
                 fundamentals=pd.DataFrame({"market_cap": {s: 5000.0 for s in closes}}),
                 meta=pd.DataFrame({"sector": {s: "IT" for s in closes}}))


def test_sma_and_field():
    p = panel_from_closes({"A": [1, 2, 3, 4, 5]})
    sma = eval_operand(Operand(fn="SMA", of="close", period=3), p)
    assert sma["A"].iloc[-1] == pytest.approx(4.0)
    close = eval_operand(Operand(field="close"), p)
    assert close["A"].iloc[-1] == 5


def test_bars_ago_shifts():
    p = panel_from_closes({"A": [1, 2, 3, 4, 5]})
    prev = eval_operand(Operand(field="close", bars_ago=2), p)
    assert prev["A"].iloc[-1] == 3


def test_rsi_extremes():
    p = panel_from_closes({"UP": list(range(1, 31)), "DN": list(range(60, 30, -1))})
    rsi = eval_operand(Operand(fn="RSI", of="close", period=14), p)
    assert rsi["UP"].iloc[-1] > 99
    assert rsi["DN"].iloc[-1] < 1


def test_expr_arithmetic():
    p = panel_from_closes({"A": [10, 10, 10, 10, 20]})
    op = Operand(expr="*", args=[Operand(const=2), Operand(field="close")])
    assert eval_operand(op, p)["A"].iloc[-1] == 40


def test_macd_components_differ():
    p = panel_from_closes({"A": [float(i) + (i % 3) for i in range(60)]})
    line = eval_operand(Operand(fn="MACD"), p)
    sig = eval_operand(Operand(fn="MACD", component="signal"), p)
    assert not np.isclose(line["A"].iloc[-1], sig["A"].iloc[-1])


def test_rolling_highest():
    p = panel_from_closes({"A": [1, 9, 2, 3, 4]})
    hh = eval_operand(Operand(fn="HIGHEST", of="close", period=3), p)
    assert hh["A"].iloc[-1] == 4
    hh5 = eval_operand(Operand(fn="HIGHEST", of="close", period=5), p)
    assert hh5["A"].iloc[-1] == 9


def test_fundamental_broadcasts():
    p = panel_from_closes({"A": [1, 2, 3]})
    mc = eval_operand(Operand(fundamental="market_cap"), p)
    assert mc.shape == p.close.shape
    assert (mc["A"] == 5000.0).all()


def test_supertrend_below_close_in_uptrend():
    p = panel_from_closes({"A": [float(100 + i) for i in range(60)]})
    st = eval_operand(Operand(fn="SUPERTREND"), p)
    assert st["A"].iloc[-1] < p.close["A"].iloc[-1]


def test_describe():
    assert describe(Operand(fn="EMA", of="close", period=20)) == "EMA(20)"
    assert describe(Operand(expr="*", args=[Operand(const=2),
                    Operand(fn="SMA", of="volume", period=20)])) == "2*SMA(volume,20)"
