from __future__ import annotations

from unittest.mock import MagicMock, patch

from apps.api.scanner.universe import enrich_universe, seed_universe
from tests.scanner_utils import make_store


def test_seed_from_bundled_csv(tmp_path):
    store = make_store(tmp_path)
    n = seed_universe(store)
    assert n > 400  # NIFTY500 snapshot
    inst = store.instruments_df()
    assert (inst["yf_symbol"].str.endswith(".NS")).all()
    assert "NIFTY500" in inst["index_memberships"].iloc[0]


def test_seed_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    seed_universe(store)
    n1 = len(store.instruments_df())
    seed_universe(store)
    assert len(store.instruments_df()) == n1


def test_enrich_writes_fundamentals(tmp_path):
    store = make_store(tmp_path, {"TCS": [100.0]})
    fake = MagicMock()
    fake.info = {"sector": "Technology", "industry": "IT Services",
                 "marketCap": 12_000_000_000_000, "trailingPE": 28.5,
                 "priceToBook": 12.0, "returnOnEquity": 0.45,
                 "dividendYield": 0.012, "trailingEps": 130.0,
                 "debtToEquity": 8.0, "revenueGrowth": 0.06}
    with patch("apps.api.scanner.universe.yf.Ticker", return_value=fake):
        n = enrich_universe(store, sleep_s=0)
    assert n == 1
    inst = store.instruments_df()
    assert inst.loc["TCS", "sector"] == "Technology"
    assert inst.loc["TCS", "fundamentals"]["pe"] == 28.5


def test_enrich_survives_per_symbol_failure(tmp_path):
    store = make_store(tmp_path, {"AAA": [1.0], "BBB": [1.0]})
    def boom_then_ok(sym):
        if sym == "AAA.NS":
            raise RuntimeError("rate limited")
        m = MagicMock(); m.info = {"sector": "X", "marketCap": 1}
        return m
    with patch("apps.api.scanner.universe.yf.Ticker", side_effect=boom_then_ok):
        n = enrich_universe(store, sleep_s=0)
    assert n == 1
