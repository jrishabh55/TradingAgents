"""Operand evaluation on wide frames (time x symbols).

Everything vectorizes across the whole universe at once; the two recursive
indicators (SUPERTREND, PSAR) loop over time but stay vectorized across
symbols, so a full-universe scan stays interactive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd

from apps.api.scanner.schema import Operand


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    fundamentals: pd.DataFrame  # index: symbol
    meta: pd.DataFrame          # index: symbol


Result = Union[pd.DataFrame, pd.Series, float, str, list]


def eval_operand(op: Operand, panel: Panel,
                 cond_eval: Optional[Callable] = None) -> Result:
    res = _eval(op, panel, cond_eval)
    if op.bars_ago and isinstance(res, pd.DataFrame):
        res = res.shift(op.bars_ago)
    return res


def _eval(op: Operand, p: Panel, cond_eval) -> Result:
    if op.const is not None:
        return float(op.const)
    if op.const_str is not None:
        return op.const_str
    if op.const_list is not None:
        return list(op.const_list)
    if op.field is not None:
        return _field(op.field, p)
    if op.fundamental is not None:
        col = p.fundamentals.get(op.fundamental)
        if col is None:
            col = pd.Series(np.nan, index=p.close.columns)
        # Broadcast the per-symbol value over time so it composes like any frame.
        return pd.DataFrame(np.tile(col.reindex(p.close.columns).to_numpy(),
                                    (len(p.close.index), 1)),
                            index=p.close.index, columns=p.close.columns)
    if op.meta is not None:
        return p.meta[op.meta] if op.meta in p.meta else pd.Series(index=p.meta.index, dtype=object)
    if op.pattern is not None:
        from apps.api.scanner.patterns import pattern_frame
        return pattern_frame(op.pattern, p)
    if op.expr is not None:
        return _expr(op, p, cond_eval)
    if op.fn is not None:
        return _fn(op, p, cond_eval)
    raise ValueError("empty operand")  # unreachable: schema validated


def _base(op: Operand, p: Panel, cond_eval) -> pd.DataFrame:
    if op.of is None or op.of == "close":
        return p.close
    if isinstance(op.of, str):
        return _field(op.of, p)
    return _eval(op.of, p, cond_eval)


def _field(name: str, p: Panel) -> pd.DataFrame:
    if name in ("open", "high", "low", "close", "volume"):
        return getattr(p, name)
    if name == "typical_price":
        return (p.high + p.low + p.close) / 3
    if name == "vwap":
        # Session-anchored: cumulative typical-price*volume per calendar day.
        tp = (p.high + p.low + p.close) / 3
        day = np.array([d.date() for d in p.close.index])
        num = (tp * p.volume).groupby(day).cumsum()
        den = p.volume.groupby(day).cumsum()
        return num / den
    if name == "change_pct":
        return p.close.pct_change() * 100
    if name == "gap_pct":
        return (p.open - p.close.shift(1)) / p.close.shift(1) * 100
    if name == "body":
        return (p.close - p.open).abs()
    if name == "upper_wick":
        return p.high - np.maximum(p.open, p.close)
    if name == "lower_wick":
        return np.minimum(p.open, p.close) - p.low
    raise ValueError(f"unknown field {name}")


def _expr(op: Operand, p: Panel, cond_eval) -> Result:
    vals = [_shifted(a, p, cond_eval) for a in op.args]
    if op.expr == "abs":
        return vals[0].abs() if isinstance(vals[0], pd.DataFrame) else abs(vals[0])
    if op.expr in ("min", "max"):
        f = np.minimum if op.expr == "min" else np.maximum
        out = vals[0]
        for v in vals[1:]:
            out = f(out, v)
        return out
    out = vals[0]
    for v in vals[1:]:
        out = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
               "*": lambda a, b: a * b, "/": lambda a, b: a / b}[op.expr](out, v)
    return out


def _shifted(op: Operand, p: Panel, cond_eval) -> Result:
    return eval_operand(op, p, cond_eval)


def _wilder(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(alpha=1 / n, adjust=False).mean()


def _sma(x, n):
    return x.rolling(n).mean()


def _ema(x, n):
    return x.ewm(span=n, adjust=False).mean()


def _wma(x: pd.DataFrame, n: int) -> pd.DataFrame:
    w = np.arange(1, n + 1, dtype=float)
    # ponytail: rolling.apply is a per-column python loop — fine at 320 rows,
    # cached by the engine; swap for a strided dot product if profiling says so.
    return x.rolling(n).apply(lambda a: np.dot(a, w) / w.sum(), raw=True)


def _true_range(p: Panel) -> pd.DataFrame:
    pc = p.close.shift(1)
    return np.maximum(p.high - p.low,
                      np.maximum((p.high - pc).abs(), (p.low - pc).abs()))


def _atr(p: Panel, n: int) -> pd.DataFrame:
    return _wilder(_true_range(p), n)


def _rsi(x: pd.DataFrame, n: int) -> pd.DataFrame:
    d = x.diff()
    up = _wilder(d.clip(lower=0), n)
    dn = _wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn)


def _stoch_k(p: Panel, n: int) -> pd.DataFrame:
    ll = p.low.rolling(n).min()
    hh = p.high.rolling(n).max()
    rng = (hh - ll).replace(0, np.nan)
    return 100 * (p.close - ll) / rng


def _bbands(x: pd.DataFrame, n: int, k: float):
    mid = _sma(x, n)
    sd = x.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def _adx(p: Panel, n: int):
    up = p.high.diff()
    dn = -p.low.diff()
    plus = up.where((up > dn) & (up > 0), 0.0)
    minus = dn.where((dn > up) & (dn > 0), 0.0)
    atr = _atr(p, n)
    pdi = 100 * _wilder(plus, n) / atr
    mdi = 100 * _wilder(minus, n) / atr
    denom = (pdi + mdi).replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / denom
    return _wilder(dx, n), pdi, mdi


def _supertrend(p: Panel, n: int, mult: float) -> pd.DataFrame:
    atr = _atr(p, n)
    hl2 = (p.high + p.low) / 2
    ub = (hl2 + mult * atr).to_numpy()
    lb = (hl2 - mult * atr).to_numpy()
    close = p.close.to_numpy()
    rows, cols = close.shape
    fub = np.array(ub, copy=True)
    flb = np.array(lb, copy=True)
    st = np.full_like(close, np.nan)
    dirn = np.ones(cols)
    for i in range(1, rows):
        # NaN prev (e.g. ATR's warmup bar) must reseed from the raw band —
        # otherwise NaN comparisons are always False and np.where carries
        # the NaN forward forever, never recovering.
        fub[i] = np.where((ub[i] < fub[i - 1]) | (close[i - 1] > fub[i - 1]) | np.isnan(fub[i - 1]),
                           ub[i], fub[i - 1])
        flb[i] = np.where((lb[i] > flb[i - 1]) | (close[i - 1] < flb[i - 1]) | np.isnan(flb[i - 1]),
                           lb[i], flb[i - 1])
        dirn = np.where(close[i] > fub[i - 1], 1.0,
                        np.where(close[i] < flb[i - 1], -1.0, dirn))
        st[i] = np.where(dirn > 0, flb[i], fub[i])
    return pd.DataFrame(st, index=p.close.index, columns=p.close.columns)


def _psar(p: Panel, step: float, cap: float) -> pd.DataFrame:
    # ponytail: standard SAR without the two-bar clamp refinement — matches
    # direction/level closely enough for scan conditions; refine if users compare
    # against TradingView values and care about the last decimal.
    h = p.high.to_numpy()
    l = p.low.to_numpy()
    rows, cols = h.shape
    out = np.full_like(h, np.nan)
    bull = np.ones(cols, dtype=bool)
    af = np.full(cols, step)
    ep = h[0].copy()
    out[0] = l[0]
    for i in range(1, rows):
        sar = out[i - 1] + af * (ep - out[i - 1])
        rev_to_bear = bull & (l[i] < sar)
        rev_to_bull = ~bull & (h[i] > sar)
        sar = np.where(rev_to_bear | rev_to_bull, ep, sar)
        new_bull = np.where(rev_to_bear, False, np.where(rev_to_bull, True, bull)).astype(bool)
        af = np.where(rev_to_bear | rev_to_bull, step, af)
        ep = np.where(rev_to_bear, l[i], np.where(rev_to_bull, h[i], ep))
        hi_ext = new_bull & (h[i] > ep)
        lo_ext = ~new_bull & (l[i] < ep)
        af = np.where(hi_ext | lo_ext, np.minimum(af + step, cap), af)
        ep = np.where(hi_ext, h[i], np.where(lo_ext, l[i], ep))
        bull = new_bull
        out[i] = sar
    return pd.DataFrame(out, index=p.close.index, columns=p.close.columns)


def _fn(op: Operand, p: Panel, cond_eval) -> pd.DataFrame:
    n = op.period
    x = _base(op, p, cond_eval)
    name = op.fn
    if name == "SMA":
        return _sma(x, n)
    if name == "EMA":
        return _ema(x, n)
    if name == "WMA":
        return _wma(x, n)
    if name == "HMA":
        return _wma(2 * _wma(x, max(n // 2, 1)) - _wma(x, n), max(int(np.sqrt(n)), 1))
    if name == "VWMA":
        vsum = p.volume.rolling(n).sum().replace(0, np.nan)
        return (x * p.volume).rolling(n).sum() / vsum
    if name == "RSI":
        return _rsi(x, n)
    if name == "STOCHRSI":
        r = _rsi(x, n)
        lo, hi = r.rolling(n).min(), r.rolling(n).max()
        return 100 * (r - lo) / (hi - lo)
    if name == "STOCH":
        k = _stoch_k(p, n)
        return _sma(k, int(op.params.get("d", 3))) if op.component == "d" else k
    if name == "CCI":
        tp = (p.high + p.low + p.close) / 3
        mad = tp.rolling(n).apply(lambda a: np.abs(a - a.mean()).mean(), raw=True)
        denom = (0.015 * mad).replace(0, np.nan)
        return (tp - _sma(tp, n)) / denom
    if name == "WILLR":
        hh, ll = p.high.rolling(n).max(), p.low.rolling(n).min()
        rng = (hh - ll).replace(0, np.nan)
        return -100 * (hh - p.close) / rng
    if name == "ROC":
        return x.pct_change(n) * 100
    if name == "MOM":
        return x.diff(n)
    if name == "MACD":
        fast = int(op.params.get("fast", 12))
        slow = int(op.params.get("slow", 26))
        sig = int(op.params.get("signal", 9))
        line = _ema(x, fast) - _ema(x, slow)
        if op.component == "signal":
            return _ema(line, sig)
        if op.component == "hist":
            return line - _ema(line, sig)
        return line
    if name == "ADX":
        adx, pdi, mdi = _adx(p, n or 14)
        return {"pdi": pdi, "mdi": mdi}.get(op.component, adx)
    if name == "SUPERTREND":
        return _supertrend(p, n or 10, op.params.get("mult", 3.0))
    if name == "PSAR":
        return _psar(p, op.params.get("step", 0.02), op.params.get("cap", 0.2))
    if name == "ATR":
        return _atr(p, n)
    if name == "BBANDS":
        u, m, l = _bbands(x, n, op.params.get("std", 2.0))
        return {"upper": u, "mid": m, "lower": l}[op.component or "upper"]
    if name == "BBWIDTH":
        u, m, l = _bbands(x, n, op.params.get("std", 2.0))
        denom = m.replace(0, np.nan)
        return (u - l) / denom * 100
    if name == "STDDEV":
        return x.rolling(n).std()
    if name == "OBV":
        return (np.sign(p.close.diff()).fillna(0) * p.volume).cumsum()
    if name == "MFI":
        tp = (p.high + p.low + p.close) / 3
        mf = tp * p.volume
        pos = mf.where(tp > tp.shift(1), 0.0).rolling(n).sum()
        neg = mf.where(tp < tp.shift(1), 0.0).rolling(n).sum()
        return 100 - 100 / (1 + pos / neg)
    if name == "CMF":
        rng = (p.high - p.low).replace(0, np.nan)
        mfv = ((p.close - p.low) - (p.high - p.close)) / rng * p.volume
        return mfv.rolling(n).sum() / p.volume.rolling(n).sum()
    if name == "HIGHEST":
        return x.rolling(n).max()
    if name == "LOWEST":
        return x.rolling(n).min()
    if name == "SUM":
        return x.rolling(n).sum()
    if name == "AVG":
        return x.rolling(n).mean()
    if name == "COUNT":
        if cond_eval is None:
            raise ValueError("COUNT needs engine context")
        return cond_eval(op.cond).astype(float).rolling(n).sum()
    raise ValueError(f"unknown function {name}")  # unreachable: schema validated


def describe(op: Operand) -> str:
    if op.const is not None:
        return f"{op.const:g}"
    if op.const_str is not None:
        return op.const_str
    if op.const_list is not None:
        return ",".join(op.const_list)
    if op.field is not None:
        base = op.field
    elif op.fundamental is not None:
        base = op.fundamental
    elif op.meta is not None:
        base = op.meta
    elif op.pattern is not None:
        base = op.pattern
    elif op.expr is not None:
        if op.expr in ("abs", "min", "max"):
            base = f"{op.expr}({','.join(describe(a) for a in op.args)})"
        else:
            base = op.expr.join(describe(a) for a in op.args)
    else:
        of = "" if op.of is None or op.of == "close" else \
            (op.of if isinstance(op.of, str) else describe(op.of)) + ","
        parts = f"{of}{op.period}" if op.period else of.rstrip(",")
        if op.params:
            params_str = ",".join(f"{k}={op.params[k]:g}" for k in sorted(op.params))
            parts = f"{parts},{params_str}" if parts else params_str
        comp = f".{op.component}" if op.component else ""
        base = f"{op.fn}({parts}){comp}"
    if op.bars_ago:
        base += f"[{op.bars_ago} ago]"
    return base
