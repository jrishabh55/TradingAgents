from __future__ import annotations

from datetime import date, datetime

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
