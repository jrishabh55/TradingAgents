"""Evaluate a scanner definition over the whole universe.

Panels load per timeframe (last ~320 bars x all symbols); every operand
computes once as a wide frame; the boolean tree reduces per-symbol at the
latest bar. Missing data never matches.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from apps.api.scanner.indicators import Panel, describe, eval_operand
from apps.api.scanner.schema import Condition, Group, Operand
from apps.api.scanner.store import ScannerStore, get_scanner_store

_RESAMPLE = {"1w": "W", "1mo": "ME"}
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


class ScanEngine:
    def __init__(self, store: ScannerStore) -> None:
        self._store = store
        self._panels: Dict[str, Panel] = {}
        self._version = -1
        self._lock = threading.Lock()

    # -- panels -----------------------------------------------------------
    def _panel(self, timeframe: str) -> Panel:
        with self._lock:
            v = self._store.version()
            if v != self._version:
                self._panels.clear()
                self._version = v
            if timeframe in self._panels:
                return self._panels[timeframe]
            base_tf = "1d" if timeframe in _RESAMPLE else timeframe
            long = self._store.load_bars(base_tf)
            inst = self._store.instruments_df()
            frames = {}
            for fld in ("open", "high", "low", "close", "volume"):
                wide = long.pivot(index="ts", columns="symbol", values=fld)
                wide.index = pd.to_datetime(wide.index)
                frames[fld] = wide.sort_index()
            if timeframe in _RESAMPLE:
                frames = {f: frames[f].resample(_RESAMPLE[timeframe]).agg(_AGG[f])
                          for f in frames}
            fund = pd.DataFrame(list(inst["fundamentals"]), index=inst.index) \
                if len(inst) else pd.DataFrame()
            if len(inst) and "market_cap" not in fund:
                fund["market_cap"] = inst["market_cap"]
            elif len(inst):
                fund["market_cap"] = fund["market_cap"].fillna(inst["market_cap"])
            meta = inst[["sector", "industry", "index_memberships", "fno"]] \
                if len(inst) else pd.DataFrame()
            panel = Panel(fundamentals=fund, meta=meta, **frames)
            self._panels[timeframe] = panel
            return panel

    # -- evaluation ---------------------------------------------------------
    def run(self, definition: Group) -> dict:
        mask, values = self._group(definition)
        d1 = self._panel("1d")
        symbols = [s for s in mask.index[mask] if s in d1.close.columns]

        close = d1.close.iloc[-1]
        prev = d1.close.iloc[-2] if len(d1.close) > 1 else close
        vol = d1.volume.iloc[-1]
        vol_avg = d1.volume.rolling(20).mean().iloc[-1]
        inst = self._store.instruments_df()

        matches = []
        for s in symbols:
            row = inst.loc[s] if s in inst.index else None
            matches.append({
                "symbol": s,
                "name": (row["name"] if row is not None else s) or s,
                "sector": row["sector"] if row is not None else None,
                "close": _r(close.get(s)),
                "change_pct": _r((close.get(s) / prev.get(s) - 1) * 100
                                 if prev.get(s) else None),
                "volume": _r(vol.get(s)),
                "rvol": _r(vol.get(s) / vol_avg.get(s)
                           if vol_avg.get(s) and vol_avg.get(s) > 0 else None),
                "values": {k: _r(v.get(s)) for k, v in values.items()},
            })
        matches.sort(key=lambda m: m["change_pct"] if m["change_pct"] is not None else -1e9,
                     reverse=True)
        data_as_of = max((str(self._store.latest_ts(tf)) for tf in ("1d", "1h", "15m", "5m")
                          if self._store.latest_ts(tf)), default="")
        return {"data_as_of": data_as_of, "universe": int(d1.close.shape[1]),
                "matches": matches}

    def _group(self, g: Group) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        masks, values = [], {}
        for child in g.children:
            m, v = (self._group(child) if isinstance(child, Group)
                    else self._condition(child))
            masks.append(m)
            values.update(v)
        idx = masks[0].index
        for m in masks[1:]:
            idx = idx.union(m.index)
        aligned = [m.reindex(idx, fill_value=False) for m in masks]
        out = aligned[0]
        for m in aligned[1:]:
            out = (out & m) if g.logic == "AND" else (out | m)
        return out, values

    def _condition(self, c: Condition) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        panel = self._panel(c.timeframe)

        # Meta conditions are time-invariant string comparisons.
        if c.left.meta is not None:
            return self._meta_condition(c, panel), {}

        frame = self._cond_frame(c, panel)
        if c.for_n_bars:
            mask = frame.tail(c.for_n_bars).all()
        else:
            mask = frame.iloc[-1] if len(frame) else pd.Series(dtype=bool)

        values: Dict[str, pd.Series] = {}
        for side in (c.left, c.right):
            if side is None or side.const is not None:
                continue
            v = eval_operand(side, panel, self._cond_frame_for(panel))
            if isinstance(v, pd.DataFrame) and len(v):
                values[f"{describe(side)} [{c.timeframe}]"] = v.iloc[-1]
        return mask.fillna(False).astype(bool), values

    def _meta_condition(self, c: Condition, panel: Panel) -> pd.Series:
        left = eval_operand(c.left, panel)
        right = eval_operand(c.right, panel)
        if c.left.meta == "index":
            vals = right if isinstance(right, list) else [right]
            mask = left.map(lambda mem: any(v in (mem or []) for v in vals))
        elif c.op == "in":
            mask = left.isin(right if isinstance(right, list) else [right])
        elif c.op == "!=":
            mask = left != right
        else:  # ==
            mask = left == right
        return mask.fillna(False).astype(bool)

    def _cond_frame_for(self, panel: Panel):
        def _eval(cond: Condition) -> pd.DataFrame:
            return self._cond_frame(cond, panel)
        return _eval

    def _cond_frame(self, c: Condition, panel: Panel) -> pd.DataFrame:
        """Raw per-bar boolean frame for a condition (validity included)."""
        cond_eval = self._cond_frame_for(panel)
        if c.left.pattern is not None:
            return eval_operand(c.left, panel, cond_eval)
        L = eval_operand(c.left, panel, cond_eval)
        R = eval_operand(c.right, panel, cond_eval)
        ops = {">": lambda a, b: a > b, "<": lambda a, b: a < b,
               ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
               "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
        if c.op == "crosses_above":
            res = _sh(L, R, lambda a, b: a > b, lambda a, b: a <= b)
        elif c.op == "crosses_below":
            res = _sh(L, R, lambda a, b: a < b, lambda a, b: a >= b)
        else:
            res = ops[c.op](L, R)
        valid = _notna(L) & _notna(R)
        if isinstance(res, pd.DataFrame):
            return (res & valid).fillna(False)
        # Scalar-vs-scalar degenerate condition: broadcast over the panel.
        return panel.close.notna() & bool(res)


def _sh(L, R, now_cmp, prev_cmp):
    Ls = L.shift(1) if isinstance(L, pd.DataFrame) else L
    Rs = R.shift(1) if isinstance(R, pd.DataFrame) else R
    return now_cmp(L, R) & prev_cmp(Ls, Rs)


def _notna(x):
    return x.notna() if isinstance(x, pd.DataFrame) else True


def _r(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v):
        return None
    return round(float(v), 4)


_engine: Optional[ScanEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> ScanEngine:
    global _engine
    with _engine_lock:
        store = get_scanner_store()
        if _engine is None or _engine._store is not store:
            _engine = ScanEngine(store)
        return _engine
