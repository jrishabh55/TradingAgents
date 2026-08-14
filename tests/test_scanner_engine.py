from __future__ import annotations

from apps.api.scanner.engine import ScanEngine
from apps.api.scanner.schema import parse_definition
from tests.scanner_utils import bars_long, make_store


def run(store, definition: dict) -> dict:
    return ScanEngine(store).run(parse_definition(definition))


def C(**kw):
    base = {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}
    base.update(kw)
    return base


def AND(*children):
    return {"logic": "AND", "children": list(children)}


def test_simple_threshold(tmp_path):
    store = make_store(tmp_path, {"HI": [101.0] * 30, "LO": [99.0] * 30})
    res = run(store, AND(C()))
    assert [m["symbol"] for m in res["matches"]] == ["HI"]
    assert res["matches"][0]["values"]["close [1d]"] == 101.0


def test_cross_fires_only_on_crossing_bar(tmp_path):
    crossing = [95.0] * 28 + [99.0, 101.0]   # crosses 100 at last bar
    above = [101.0] * 30                      # already above — no cross
    store = make_store(tmp_path, {"X": crossing, "A": above})
    res = run(store, AND(C(op="crosses_above")))
    assert [m["symbol"] for m in res["matches"]] == ["X"]


def test_for_n_bars_streak(tmp_path):
    store = make_store(tmp_path, {"S": [99.0] * 27 + [101, 102, 103],
                                  "N": [99.0] * 28 + [101, 102]})
    res = run(store, AND(dict(C(), for_n_bars=3)))
    assert [m["symbol"] for m in res["matches"]] == ["S"]


def test_or_group_and_nesting(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [50.0] * 30, "C": [99.0] * 30})
    d = {"logic": "OR", "children": [
        C(), C(op="<", right={"const": 60}),
    ]}
    res = run(store, d)
    assert {m["symbol"] for m in res["matches"]} == {"A", "B"}


def test_missing_data_excluded(tmp_path):
    store = make_store(tmp_path, {"FULL": [101.0] * 300, "SHORT": [101.0] * 5})
    d = AND(C(left={"fn": "SMA", "of": "close", "period": 200}, op=">",
              right={"const": 0}))
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["FULL"]


def test_multi_timeframe_and(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [101.0] * 30})
    store.upsert_bars("15m", bars_long("A", [201.0] * 30, freq_minutes=15))
    store.upsert_bars("15m", bars_long("B", [10.0] * 30, freq_minutes=15))
    d = AND(C(), C(timeframe="15m", right={"const": 200}))
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["A"]


def test_meta_condition(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30})
    d = AND(C(left={"meta": "sector"}, op="==", right={"const_str": "Test"}))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]
    d2 = AND(C(left={"meta": "sector"}, op="==", right={"const_str": "Banking"}))
    assert run(store, d2)["matches"] == []


def test_meta_not_equal_excludes_null_values(tmp_path):
    # Regression: object-dtype `None != "X"` is True in pandas, so a null
    # sector/industry (common now that the universe covers all NSE
    # mainboard equities, not just the enriched NIFTY500) would otherwise
    # match every "!=" condition instead of being excluded.
    store = make_store(tmp_path)
    store.upsert_instruments([
        {"symbol": "BANK", "yf_symbol": "BANK.NS", "name": "BANK", "sector": "Banking",
         "industry": "Banking", "market_cap": 5000.0, "index_memberships": ["NIFTY500"],
         "fno": False, "fundamentals": {}},
        {"symbol": "NULLSEC", "yf_symbol": "NULLSEC.NS", "name": "NULLSEC", "sector": None,
         "industry": None, "market_cap": 5000.0, "index_memberships": [],
         "fno": False, "fundamentals": {}},
    ])
    for sym in ("BANK", "NULLSEC"):
        store.upsert_bars("1d", bars_long(sym, [101.0] * 30))
    d = AND(C(left={"meta": "sector"}, op="!=", right={"const_str": "IT"}))
    matches = {m["symbol"] for m in run(store, d)["matches"]}
    assert matches == {"BANK"}


def test_fundamental_condition(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30})
    d = AND(C(left={"fundamental": "market_cap"}, op=">", right={"const": 1000}))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]


def test_weekly_resample(tmp_path):
    store = make_store(tmp_path, {"A": [float(100 + i) for i in range(60)]})
    d = AND(C(timeframe="1w"))
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]


def test_result_shape(tmp_path):
    store = make_store(tmp_path, {"A": [100.0] * 29 + [110.0]})
    res = run(store, AND(C()))
    m = res["matches"][0]
    assert m["name"] == "A" and m["sector"] == "Test"
    assert m["change_pct"] == 10.0
    assert m["rvol"] == 1.0
    assert res["data_as_of"]


def _seed_instruments(store, symbols):
    store.upsert_instruments([
        {"symbol": s, "yf_symbol": f"{s}.NS", "name": s, "sector": "Test",
         "industry": "Test", "market_cap": 5000.0, "index_memberships": ["NIFTY500"],
         "fno": False, "fundamentals": {"pe": 20.0}}
        for s in symbols
    ])


def test_count_streak(tmp_path):
    store = make_store(tmp_path)
    _seed_instruments(store, ["S", "N"])
    # S: 4 green bars out of last 5 (bar2 is red).
    store.upsert_bars("1d", bars_long("S", [105, 103, 108, 112, 115],
                                      opens=[100, 105, 103, 108, 112]))
    # N: 2 green bars out of last 5.
    store.upsert_bars("1d", bars_long("N", [105, 100, 95, 100, 90],
                                      opens=[100, 105, 100, 95, 100]))
    inner = {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"field": "open"}}
    d = AND({"timeframe": "1d",
             "left": {"fn": "COUNT", "cond": inner, "period": 5},
             "op": ">=", "right": {"const": 4}})
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["S"]


def test_group_in_group(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [50.0] * 30, "C": [99.0] * 30})
    inner_or = {"logic": "OR", "children": [C(), C(op="<", right={"const": 60})]}
    always_true = C(left={"field": "volume"}, op=">", right={"const": 0})
    d = AND(inner_or, always_true)
    res = run(store, d)
    assert {m["symbol"] for m in res["matches"]} == {"A", "B"}


def test_panel_cache_invalidation(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30})
    engine = ScanEngine(store)
    definition = parse_definition(AND(C()))

    res1 = engine.run(definition)
    assert [m["symbol"] for m in res1["matches"]] == ["A"]

    store.upsert_bars("1d", bars_long("A", [50.0] * 30))

    res2 = engine.run(definition)
    assert res2["matches"] == []


def test_get_engine_rebinds(tmp_path):
    from apps.api.scanner.engine import get_engine
    from apps.api.scanner.store import reset_scanner_store_for_tests

    store1 = reset_scanner_store_for_tests(tmp_path / "a.sqlite")
    engine1 = get_engine()
    assert engine1._store is store1

    store2 = reset_scanner_store_for_tests(tmp_path / "b.sqlite")
    engine2 = get_engine()
    assert engine2._store is store2
    assert engine2._store is not store1


def test_meta_index_membership_matches(tmp_path):
    # scanner_utils seeds index_memberships=["NIFTY500"] for every symbol.
    store = make_store(tmp_path, {"A": [101.0] * 30})
    d = AND({"timeframe": "1d", "left": {"meta": "index"}, "op": "in",
             "right": {"const_list": ["NIFTY500"]}})
    assert [m["symbol"] for m in run(store, d)["matches"]] == ["A"]


def test_meta_index_empty_membership_no_match_no_crash(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [101.0] * 30})
    store.upsert_instruments([
        {"symbol": "B", "yf_symbol": "B.NS", "name": "B", "sector": "Test",
         "industry": "Test", "market_cap": 5000.0, "index_memberships": [],
         "fno": False, "fundamentals": {"pe": 20.0}},
    ])
    d = AND({"timeframe": "1d", "left": {"meta": "index"}, "op": "in",
             "right": {"const_list": ["NIFTY500"]}})
    res = run(store, d)
    assert [m["symbol"] for m in res["matches"]] == ["A"]


def test_run_empty_bars_returns_empty_result(tmp_path):
    store = make_store(tmp_path)
    _seed_instruments(store, ["A", "B"])  # instruments exist, zero bars
    res = run(store, AND(C()))
    assert res == {"data_as_of": "", "universe": 0, "matches": []}


def test_multi_timeframe_asymmetric_symbols(tmp_path):
    store = make_store(tmp_path, {"A": [101.0] * 30, "B": [101.0] * 30})
    store.upsert_bars("15m", bars_long("A", [201.0] * 30, freq_minutes=15))
    # B has no 15m bars at all.

    d_and = AND(C(), C(timeframe="15m", right={"const": 200}))
    res_and = run(store, d_and)
    assert [m["symbol"] for m in res_and["matches"]] == ["A"]

    d_or = {"logic": "OR", "children": [C(), C(timeframe="15m", right={"const": 200})]}
    res_or = run(store, d_or)
    assert {m["symbol"] for m in res_or["matches"]} == {"A", "B"}
