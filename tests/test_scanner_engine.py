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
