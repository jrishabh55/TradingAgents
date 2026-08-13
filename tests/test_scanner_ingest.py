from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from apps.api.scanner.ingest import refresh_timeframe
from tests.scanner_utils import make_store


def yf_frame(symbols: list[str], closes: list[float]) -> pd.DataFrame:
    """Mimic yf.download(group_by='ticker') multi-index output."""
    idx = pd.date_range("2026-08-03", periods=len(closes), freq="D")
    frames = {}
    for s in symbols:
        frames[s] = pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes],
                                  "Low": [c * 0.99 for c in closes], "Close": closes,
                                  "Volume": [1000] * len(closes)}, index=idx)
    return pd.concat(frames, axis=1)


def test_refresh_writes_bars(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0], "INFY": [1.0]})
    data = yf_frame(["TCS.NS", "INFY.NS"], [100.0, 101.0, 102.0])
    with patch("apps.api.scanner.ingest.yf.download", return_value=data) as dl:
        n = refresh_timeframe(store, "1d")
    assert n == 6  # 2 symbols x 3 bars
    df = store.load_bars("1d")
    assert set(df["symbol"]) == {"TCS", "INFY"}
    assert dl.call_args.kwargs["interval"] == "1d"


def test_refresh_skips_symbols_with_no_data(tmp_path):
    # Seed instruments only (no pre-existing bars) — make_store's `data` dict
    # also seeds a bar per symbol, which would leave a stale DEAD row in the
    # store independent of the mocked NaN response below and confound the
    # assertion.
    store = make_store(tmp_path)
    store.upsert_instruments([
        {"symbol": "TCS", "yf_symbol": "TCS.NS", "name": "TCS"},
        {"symbol": "DEAD", "yf_symbol": "DEAD.NS", "name": "DEAD"},
    ])
    data = yf_frame(["TCS.NS", "DEAD.NS"], [100.0, 101.0])
    data[("DEAD.NS", "Close")] = float("nan")
    with patch("apps.api.scanner.ingest.yf.download", return_value=data):
        refresh_timeframe(store, "1d")
    assert set(store.load_bars("1d")["symbol"]) == {"TCS"}


def test_refresh_prunes_to_retention(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0]})
    data = yf_frame(["TCS.NS"], [float(i) for i in range(400)])
    with patch("apps.api.scanner.ingest.yf.download", return_value=data):
        refresh_timeframe(store, "1d")
    assert len(store.load_bars("1d", limit=1000)) == 320
