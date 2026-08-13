from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from apps.api.scanner.calendar import IST, is_market_open, is_trading_day


def test_weekend_closed():
    assert not is_trading_day(date(2026, 8, 15))  # Saturday (also Independence Day)
    assert not is_trading_day(date(2026, 8, 16))  # Sunday


def test_holiday_closed():
    assert not is_trading_day(date(2026, 1, 26))  # Republic Day


def test_weekday_open_hours():
    assert is_trading_day(date(2026, 8, 13))  # Thursday
    assert is_market_open(datetime(2026, 8, 13, 10, 0, tzinfo=IST))
    assert not is_market_open(datetime(2026, 8, 13, 9, 0, tzinfo=IST))
    assert not is_market_open(datetime(2026, 8, 13, 15, 45, tzinfo=IST))


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="is_market_open requires a timezone-aware datetime"):
        is_market_open(datetime(2026, 8, 13, 10, 0))


def test_aware_utc_converts():
    # 04:45 UTC == 10:15 IST (market just opened)
    assert is_market_open(datetime(2026, 8, 13, 4, 45, tzinfo=timezone.utc))
    # 11:00 UTC == 16:30 IST (market just closed)
    assert not is_market_open(datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc))


def test_boundaries_inclusive():
    # Exactly at 09:15 (open)
    assert is_market_open(datetime(2026, 8, 13, 9, 15, tzinfo=IST))
    # One minute before 09:15
    assert not is_market_open(datetime(2026, 8, 13, 9, 14, tzinfo=IST))
    # Exactly at 15:30 (close)
    assert is_market_open(datetime(2026, 8, 13, 15, 30, tzinfo=IST))
    # One minute after 15:30
    assert not is_market_open(datetime(2026, 8, 13, 15, 31, tzinfo=IST))
