"""Evaluate a scanner definition over the whole universe.

Panels load per timeframe (last ~320 bars x all symbols); every operand
computes once as a wide frame; the boolean tree reduces per-symbol at the
latest bar. Missing data never matches.

Each `run()` resolves a local snapshot of every panel it needs up front (see
`_resolve_panels`), so a bar-version bump that lands mid-run can never make
two calls within the same run() see different data — the instance-level
cache is only consulted/refreshed once, before evaluation starts.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional, Set, Tuple

import numpy as np
import pandas as pd

from apps.api.scanner.indicators import Panel, describe, eval_operand
from apps.api.scanner.schema import Condition, Group, Operand
from apps.api.scanner.store import ScannerStore, get_scanner_store

_RESAMPLE = {"1w": "W", "1mo": "ME"}
# ponytail: %chg reports on the coarsest timeframe in the definition (1d floor),
# so a 1w filter shows weekly change instead of a misleading daily one.
_TF_ORDER = ("5m", "15m", "1h", "1d", "1w", "1mo")
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

PanelMap = Dict[str, Panel]


class ScanEngine:
    def __init__(self, store: ScannerStore) -> None:
        self._store = store
        self._panels: Dict[str, Panel] = {}
        self._version = -1
        self._lock = threading.Lock()

    # -- panels -----------------------------------------------------------
    def _build_panel(self, timeframe: str) -> Panel:
        """Construct a fresh Panel for `timeframe` from the store. No caching,
        no locking, no version check — callers own that (see _resolve_panels)."""
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
        return Panel(fundamentals=fund, meta=meta, **frames)

    def warm(self) -> None:
        """Pre-build every panel after an ingest cycle so no user request pays
        the cold rebuild (~seconds at full-universe size). Called from the
        ingest loop right after the cycle's version bump. Builds run OUTSIDE
        the lock — concurrent scans keep serving the previous snapshot (or
        rebuild for themselves if they observed the bump first) — then the
        cache swaps in one atomic assignment."""
        v = self._store.version()
        fresh = {tf: self._build_panel(tf)
                 for tf in ("1d", "1h", "15m", "5m", "1w", "1mo")}
        with self._lock:
            self._panels = fresh
            self._version = v

    def _resolve_panels(self, timeframes: Set[str]) -> PanelMap:
        """Per-run snapshot: check the store version exactly once, refresh the
        instance cache if stale, then return a local dict covering exactly the
        timeframes this run needs. No version re-check happens after this."""
        with self._lock:
            v = self._store.version()
            if v != self._version:
                self._panels.clear()
                self._version = v
            panels: PanelMap = {}
            for tf in timeframes:
                if tf not in self._panels:
                    self._panels[tf] = self._build_panel(tf)
                panels[tf] = self._panels[tf]
            return panels

    # -- evaluation ---------------------------------------------------------
    def run(self, definition: Group) -> dict:
        timeframes = _collect_timeframes(definition)
        panels = self._resolve_panels(timeframes)

        d1 = panels["1d"]
        if d1.close.shape[0] == 0 or d1.close.shape[1] == 0:
            # No bars (fresh deploy / empty universe) — short-circuit before
            # mask evaluation, which is not safe to run over empty frames.
            return {"data_as_of": "", "universe": 0, "matches": []}

        mask, values = self._group(definition, panels)
        symbols = [s for s in mask.index[mask] if s in d1.close.columns]

        close = d1.close.iloc[-1]
        chg_tf = max(timeframes, key=_TF_ORDER.index)
        if chg_tf in ("5m", "15m", "1h"):
            chg_tf = "1d"  # intraday bar-over-bar change is noise; keep daily
        tf_close = panels[chg_tf].close
        chg_now = tf_close.iloc[-1]
        prev = tf_close.iloc[-2] if len(tf_close) > 1 else chg_now
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
                "change_pct": _r((chg_now.get(s) / prev.get(s) - 1) * 100
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
                "change_tf": chg_tf, "matches": matches}

    def _group(self, g: Group, panels: PanelMap) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        masks, values = [], {}
        for child in g.children:
            m, v = (self._group(child, panels) if isinstance(child, Group)
                    else self._condition(child, panels))
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

    def _condition(self, c: Condition, panels: PanelMap) -> Tuple[pd.Series, Dict[str, pd.Series]]:
        panel = panels[c.timeframe]

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
            # `mem` may be NaN/None for instruments with no membership data
            # (missing enrichment, or a genuinely empty list) — treat both
            # as "member of nothing" rather than crashing on `in NaN`.
            mask = left.map(lambda mem: any(v in mem for v in vals)
                            if isinstance(mem, list) else False)
        elif c.op == "in":
            mask = left.isin(right if isinstance(right, list) else [right])
        elif c.op == "!=":
            # None != "X" is True for object-dtype Series, so a null
            # sector/industry would otherwise match every "!=" condition —
            # require non-null too.
            mask = (left != right) & left.notna()
        else:  # ==
            mask = left == right
        return mask.fillna(False).astype(bool)

    def _cond_frame_for(self, panel: Panel):
        def _eval(cond: Condition) -> pd.DataFrame:
            return self._cond_frame(cond, panel)
        return _eval

    def _cond_frame(self, c: Condition, panel: Panel) -> pd.DataFrame:
        """Raw per-bar boolean frame for a condition (validity included).

        `panel` is the panel for c.timeframe. A COUNT operand's inner
        condition is validated (schema.Condition._shape) to share its outer
        condition's timeframe, so reusing the same `panel` here for that
        nested evaluation is always correct — never a different timeframe's
        data leaking in.
        """
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


# -- timeframe collection (per-run snapshot planning) ------------------------

def _operand_timeframes(op: Optional[Operand]) -> Set[str]:
    """Timeframes referenced transitively by an operand: only COUNT operands
    (via their nested condition) actually carry a timeframe; walk into
    `of`-operands and expr `args` to find any COUNT operands nested there."""
    if op is None:
        return set()
    tfs: Set[str] = set()
    if op.fn == "COUNT" and op.cond is not None:
        tfs |= _condition_timeframes(op.cond)
    if isinstance(op.of, Operand):
        tfs |= _operand_timeframes(op.of)
    if op.args:
        for a in op.args:
            tfs |= _operand_timeframes(a)
    return tfs


def _condition_timeframes(c: Condition) -> Set[str]:
    tfs = {c.timeframe}
    tfs |= _operand_timeframes(c.left)
    tfs |= _operand_timeframes(c.right)
    return tfs


def _group_timeframes(g: Group) -> Set[str]:
    tfs: Set[str] = set()
    for child in g.children:
        tfs |= _group_timeframes(child) if isinstance(child, Group) else _condition_timeframes(child)
    return tfs


def _collect_timeframes(g: Group) -> Set[str]:
    """Every timeframe this definition needs a panel for, always including
    "1d" (run() reports change/volume/rvol/universe off the 1d panel)."""
    return _group_timeframes(g) | {"1d"}


_engine: Optional[ScanEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> ScanEngine:
    global _engine
    with _engine_lock:
        store = get_scanner_store()
        if _engine is None or _engine._store is not store:
            _engine = ScanEngine(store)
        return _engine
