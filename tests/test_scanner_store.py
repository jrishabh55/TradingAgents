from __future__ import annotations

from tests.scanner_utils import bars_long, make_store


def test_bars_roundtrip_and_version(tmp_path):
    store = make_store(tmp_path)
    assert store.version() == 0
    assert store.latest_ts("1d") is None
    store.upsert_bars("1d", bars_long("TCS", [100, 101, 102]))
    assert store.version() == 1
    df = store.load_bars("1d")
    assert list(df["close"]) == [100, 101, 102]
    assert df["ts"].is_monotonic_increasing
    assert store.latest_ts("1d") == df["ts"].iloc[-1]
    # Idempotent upsert: same PK rows replace, not duplicate.
    store.upsert_bars("1d", bars_long("TCS", [100, 101, 102]))
    assert len(store.load_bars("1d")) == 3


def test_prune_keeps_newest(tmp_path):
    store = make_store(tmp_path)
    store.upsert_bars("1d", bars_long("TCS", list(range(400))))
    store.prune_bars("1d", keep=320)
    df = store.load_bars("1d", limit=500)
    assert len(df) == 320
    assert df["close"].iloc[-1] == 399


def test_instruments_roundtrip(tmp_path):
    store = make_store(tmp_path, {"TCS": [1.0]})
    inst = store.instruments_df()
    assert inst.loc["TCS", "yf_symbol"] == "TCS.NS"
    assert inst.loc["TCS", "index_memberships"] == ["NIFTY500"]
    assert inst.loc["TCS", "fundamentals"]["pe"] == 20.0


def test_scanner_crud_and_scoping(tmp_path):
    store = make_store(tmp_path)
    d = {"logic": "AND", "children": []}
    sid = store.create_scanner("user_a", "My scan", "desc", d)
    store.upsert_prebuilt("Golden cross", "pb", d)
    store.upsert_prebuilt("Golden cross", "pb updated", d)  # keyed by name

    a = store.list_scanners("user_a")
    b = store.list_scanners("user_b")
    assert {s["name"] for s in a} == {"My scan", "Golden cross"}
    assert {s["name"] for s in b} == {"Golden cross"}
    assert store.get_scanner(sid)["user_id"] == "user_a"

    store.update_scanner(sid, "Renamed", "d2", d)
    assert store.get_scanner(sid)["name"] == "Renamed"
    store.delete_scanner(sid)
    assert store.get_scanner(sid) is None
