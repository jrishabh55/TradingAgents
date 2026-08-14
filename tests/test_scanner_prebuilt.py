from __future__ import annotations

from apps.api.scanner.seed import seed_prebuilt
from tests.scanner_utils import make_store


def test_all_prebuilt_seed_and_validate(tmp_path):
    store = make_store(tmp_path)
    n = seed_prebuilt(store)
    assert n == 19
    names = {s["name"] for s in store.list_scanners("nobody")}
    assert "Golden cross" in names and "Volume spike" in names
    assert "Death cross" in names and "Weekly momentum" in names
    # Idempotent: re-seed updates, never duplicates.
    seed_prebuilt(store)
    assert len(store.list_scanners("nobody")) == 19
