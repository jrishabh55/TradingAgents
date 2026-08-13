"""Scheduled yfinance ingest: EOD after close, delayed intraday during hours.

Runs as an asyncio task inside the API process (same pattern as the jobs
runner). yfinance intraday is 15-20 min delayed — that's the accepted product
trade-off; see the spec. All fetch work happens in a thread via
asyncio.to_thread so the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pandas as pd
import yfinance as yf

from apps.api.scanner.calendar import IST, is_market_open, is_trading_day
from apps.api.scanner.store import ScannerStore, get_scanner_store
from apps.api.scanner.universe import enrich_universe, seed_universe

logger = logging.getLogger(__name__)

#: timeframe -> (yfinance period, yfinance interval)
TF_FETCH = {"1d": ("2y", "1d"), "1h": ("730d", "1h"),
            "15m": ("60d", "15m"), "5m": ("60d", "5m")}
CHUNK = 100
RETENTION = 320
INTRADAY_REFRESH_SECONDS = 600
EOD_HOUR_IST = 18  # refresh daily bars after 18:00 IST


def refresh_timeframe(store: ScannerStore, timeframe: str) -> int:
    period, interval = TF_FETCH[timeframe]
    inst = store.instruments_df()
    if inst.empty:
        return 0
    yf_to_ours = dict(zip(inst["yf_symbol"], inst.index))
    written = 0
    yf_symbols = list(yf_to_ours)
    for i in range(0, len(yf_symbols), CHUNK):
        chunk = yf_symbols[i:i + CHUNK]
        try:
            data = yf.download(tickers=" ".join(chunk), period=period,
                               interval=interval, group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
            long = _to_long(data, chunk, yf_to_ours)
            if not long.empty:
                store.upsert_bars(timeframe, long)
                written += len(long)
        except Exception as exc:  # noqa: BLE001 — partial universe beats no universe
            logger.warning("yf.download %s chunk %d failed: %s", timeframe, i, exc)
            continue
    store.prune_bars(timeframe, keep=RETENTION)
    return written


def _to_long(data: pd.DataFrame, chunk: list[str], yf_to_ours: dict) -> pd.DataFrame:
    frames = []
    for yf_sym in chunk:
        try:
            df = data[yf_sym] if isinstance(data.columns, pd.MultiIndex) else data
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        frames.append(pd.DataFrame({
            "symbol": yf_to_ours[yf_sym],
            "ts": [t.isoformat() for t in df.index],
            "open": df["Open"].to_numpy(), "high": df["High"].to_numpy(),
            "low": df["Low"].to_numpy(), "close": df["Close"].to_numpy(),
            "volume": df["Volume"].to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def refresh_all(store: ScannerStore) -> None:
    for tf in TF_FETCH:
        n = refresh_timeframe(store, tf)
        logger.info("scanner ingest: %s -> %d bars", tf, n)


async def ingest_loop() -> None:
    store = get_scanner_store()
    if store.instruments_df().empty:
        await asyncio.to_thread(seed_universe, store)
        logger.info("scanner universe seeded")
    if store.latest_ts("1d") is None:
        logger.info("scanner initial backfill starting")
        await asyncio.to_thread(refresh_all, store)

    last_intraday = 0.0
    eod_done_for = ""
    enrich_done_for = ""
    while True:
        try:
            now = datetime.now(IST)
            loop_t = asyncio.get_running_loop().time()
            if is_market_open(now) and loop_t - last_intraday > INTRADAY_REFRESH_SECONDS:
                last_intraday = loop_t
                for tf in ("5m", "15m", "1h"):
                    await asyncio.to_thread(refresh_timeframe, store, tf)
            today = now.date().isoformat()
            if (is_trading_day(now.date()) and now.hour >= EOD_HOUR_IST
                    and eod_done_for != today):
                eod_done_for = today
                await asyncio.to_thread(refresh_timeframe, store, "1d")
            # Weekly fundamentals sweep on Saturdays.
            week = f"{now.isocalendar().year}-{now.isocalendar().week}"
            if now.weekday() == 5 and enrich_done_for != week:
                enrich_done_for = week
                await asyncio.to_thread(enrich_universe, store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive any single failure
            logger.exception("scanner ingest cycle failed")
        await asyncio.sleep(60)
