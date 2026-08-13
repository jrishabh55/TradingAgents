"""Synthetic-bar helpers shared by scanner tests."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from apps.api.scanner.store import ScannerStore, reset_scanner_store_for_tests


def bars_long(symbol: str, closes: list[float], *, start: str = "2026-01-05",
              volumes: list[float] | None = None, freq_minutes: int | None = None,
              opens: list[float] | None = None, highs: list[float] | None = None,
              lows: list[float] | None = None) -> pd.DataFrame:
    """Long-format bars from a close series. Daily by default; freq_minutes for intraday."""
    n = len(closes)
    t0 = datetime.fromisoformat(start)
    if freq_minutes:
        ts = [(t0 + timedelta(minutes=freq_minutes * i)).isoformat() for i in range(n)]
    else:
        ts = [(t0 + timedelta(days=i)).isoformat() for i in range(n)]
    o = opens if opens is not None else [closes[max(i - 1, 0)] for i in range(n)]
    h = highs if highs is not None else [max(a, b) * 1.001 for a, b in zip(o, closes)]
    l = lows if lows is not None else [min(a, b) * 0.999 for a, b in zip(o, closes)]
    v = volumes if volumes is not None else [1000.0] * n
    return pd.DataFrame({"symbol": symbol, "ts": ts, "open": o, "high": h,
                         "low": l, "close": closes, "volume": v})


def make_store(tmp_path, data: dict[str, list[float]] | None = None,
               timeframe: str = "1d") -> ScannerStore:
    """Fresh store; optionally seed instruments + bars from {symbol: closes}."""
    store = reset_scanner_store_for_tests(tmp_path / "scanner.sqlite")
    if data:
        store.upsert_instruments([
            {"symbol": s, "yf_symbol": f"{s}.NS", "name": s, "sector": "Test",
             "industry": "Test", "market_cap": 5000.0, "index_memberships": ["NIFTY500"],
             "fno": False, "fundamentals": {"pe": 20.0}}
            for s in data
        ])
        for s, closes in data.items():
            store.upsert_bars(timeframe, bars_long(s, closes))
    return store
