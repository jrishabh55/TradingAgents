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

#: All-listed-equities master file — the source of truth for the universe.
#: If this one is unreachable, refresh_universe_csv() bails out entirely
#: rather than fabricate a CSV from partial data.
NSE_EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
#: Index membership tag sources, tried in order with a niftyindices.com
#: fallback mirror if the NSE archive blocks the request.
NSE_NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_NIFTY50_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTYINDICES_NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
NIFTYINDICES_NIFTY50_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"

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
                "index_memberships": r["index_memberships"].split("|") if r["index_memberships"] else [],
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


def _fetch_nse_csv(url: str, *, retries: int = 1) -> list[dict[str, str]] | None:
    """GET+parse an NSE archive CSV. Retries once (per the NSE-blocks-you
    pattern seen in prod), then gives up and returns None rather than
    raising — callers decide whether that's fatal or falls back to a mirror.
    Keys/values are stripped since NSE's CSVs pad header names with spaces
    (e.g. " SERIES")."""
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(raw)))
            return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]
        except Exception as exc:  # noqa: BLE001 — one bad fetch must not kill the refresh
            logger.warning("fetch %s failed (attempt %d/%d): %s", url, attempt + 1, attempts, exc)
    return None


def refresh_universe_csv() -> str:
    """Dev utility: regenerate the bundled CSV from NSE archives.

    Builds the universe from EQUITY_L.csv (all listed equities, filtered to
    SERIES == "EQ") and tags each symbol with its index memberships from the
    NIFTY50/NIFTY500 constituent lists (niftyindices.com mirrors are tried
    if the NSE archive blocks the index-list requests). If EQUITY_L itself
    can't be fetched there's no safe partial universe to fall back to, so
    this stops and returns "BLOCKED" rather than fabricate data — callers
    must not overwrite the bundled CSV in that case.

    Returns "OK" on success, "BLOCKED" if the master equity list couldn't
    be fetched at all.
    """
    equity_rows = _fetch_nse_csv(NSE_EQUITY_URL)
    if equity_rows is None:
        logger.error("refresh_universe_csv: EQUITY_L.csv unreachable after retry, aborting")
        return "BLOCKED"

    nifty500_rows = _fetch_nse_csv(NSE_NIFTY500_URL) or _fetch_nse_csv(NIFTYINDICES_NIFTY500_URL, retries=0)
    nifty50_rows = _fetch_nse_csv(NSE_NIFTY50_URL) or _fetch_nse_csv(NIFTYINDICES_NIFTY50_URL, retries=0)
    if nifty500_rows is None:
        logger.warning("refresh_universe_csv: NIFTY500 list unreachable, index_memberships/industry will be sparse")
    if nifty50_rows is None:
        logger.warning("refresh_universe_csv: NIFTY50 list unreachable, NIFTY50 tags will be missing")

    industry_by_symbol: dict[str, str] = {}
    memberships: dict[str, set[str]] = {}
    for r in nifty500_rows or []:
        sym = r.get("Symbol", "").strip()
        if not sym:
            continue
        industry_by_symbol[sym] = r.get("Industry", "").strip()
        memberships.setdefault(sym, set()).add("NIFTY500")
    for r in nifty50_rows or []:
        sym = r.get("Symbol", "").strip()
        if not sym:
            continue
        memberships.setdefault(sym, set()).add("NIFTY50")

    out_rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for r in equity_rows:
        if r.get("SERIES", "").strip() != "EQ":
            continue
        symbol = r.get("SYMBOL", "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = r.get("NAME OF COMPANY", "").strip()
        industry = industry_by_symbol.get(symbol, "")
        tags = memberships.get(symbol, set())
        # Fixed tag order (NIFTY50 before NIFTY500) so the pipe-joined
        # column is deterministic across regenerations.
        tag_str = "|".join(t for t in ("NIFTY50", "NIFTY500") if t in tags)
        out_rows.append((symbol, name, industry, tag_str))

    out_rows.sort(key=lambda r: r[0])
    with DATA_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "industry", "index_memberships"])
        w.writerows(out_rows)
    logger.info("refresh_universe_csv: wrote %d rows", len(out_rows))
    return "OK"
