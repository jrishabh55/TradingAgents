"""NSE universe: bundled NIFTY500 snapshot + weekly yfinance enrichment.

The CSV ships in the repo so a fresh deploy scans immediately without
depending on NSE archives being reachable from a datacenter IP.
refresh_universe_csv() regenerates it — run by hand, review the diff, commit.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from pathlib import Path

import yfinance as yf

from apps.api.scanner.store import ScannerStore

logger = logging.getLogger(__name__)

DATA_CSV = Path(__file__).parent / "data" / "nse_universe.csv"
NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

#: yfinance info key -> our fundamentals key
_FUND_KEYS = {"trailingPE": "pe", "priceToBook": "pb", "returnOnEquity": "roe",
              "dividendYield": "dividend_yield", "trailingEps": "eps",
              "debtToEquity": "debt_to_equity", "revenueGrowth": "revenue_growth"}


def seed_universe(store: ScannerStore) -> int:
    rows = []
    with DATA_CSV.open() as f:
        for r in csv.DictReader(f):
            rows.append({
                "symbol": r["symbol"],
                "yf_symbol": f"{r['symbol']}.NS",
                "name": r["name"],
                "industry": r["industry"] or None,
                "index_memberships": r["index_memberships"].split("|"),
            })
    store.upsert_instruments(rows)
    return len(rows)


def enrich_universe(store: ScannerStore, limit: int | None = None,
                    sleep_s: float = 0.25) -> int:
    """Fill sector/mcap/fundamentals from yfinance. Weekly cadence; failures skip."""
    inst = store.instruments_df()
    done = 0
    for symbol, row in inst.iterrows():
        if limit is not None and done >= limit:
            break
        try:
            info = yf.Ticker(row["yf_symbol"]).info or {}
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the sweep
            logger.warning("enrich %s failed: %s", symbol, exc)
            continue
        fundamentals = {ours: info[theirs] for theirs, ours in _FUND_KEYS.items()
                        if info.get(theirs) is not None}
        store.upsert_instruments([{
            "symbol": symbol,
            "yf_symbol": row["yf_symbol"],
            "name": row["name"],
            "sector": info.get("sector"),
            "industry": info.get("industry") or row.get("industry"),
            "market_cap": info.get("marketCap"),
            "index_memberships": row["index_memberships"],
            "fno": bool(row.get("fno", False)),
            "fundamentals": fundamentals,
        }])
        done += 1
        if sleep_s and limit is None:
            time.sleep(sleep_s)  # be polite to Yahoo on the full sweep
    return done


def refresh_universe_csv() -> None:
    """Dev utility: regenerate the bundled CSV from NSE archives."""
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    with DATA_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "industry", "index_memberships"])
        for r in rows:
            w.writerow([r["Symbol"].strip(), r["Company Name"].strip(),
                        r.get("Industry", "").strip(), "NIFTY500"])
