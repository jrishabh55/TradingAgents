"""NSE market calendar: fixed hours + a static holiday list.

ponytail: static holiday list, refreshed by hand each January from NSE's
published circular. A wrong entry costs one skipped/extra ingest cycle, not
data corruption — upgrade to an exchange-calendar library only if that ever
actually bites.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE trading holidays 2026. Fixed national dates are certain; VERIFY the
# movable ones (Holi, Eid, Diwali, etc.) against NSE's 2026 circular at
# https://www.nseindia.com/resources/exchange-communication-holidays
HOLIDAYS: set[str] = {
    "2026-01-26",  # Republic Day
    "2026-03-04",  # Holi (verify)
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day (Saturday in 2026)
    "2026-10-02",  # Gandhi Jayanti
    "2026-11-09",  # Diwali (verify)
    "2026-12-25",  # Christmas
}

OPEN = time(9, 15)
CLOSE = time(15, 30)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def is_market_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(IST)).astimezone(IST)
    return is_trading_day(now.date()) and OPEN <= now.time() <= CLOSE
