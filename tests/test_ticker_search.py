"""GET /api/search/tickers — Yahoo typeahead proxy (apps/api/routes/config.py).

Yahoo is mocked out: these pin the response shape the frontend consumes and
the degrade-don't-500 contract, not Yahoo's actual data.
"""
from unittest.mock import MagicMock, patch

from apps.api.routes.config import search_tickers


def _quote(**kw):
    base = {"symbol": "AAPL", "shortname": "Apple Inc.", "exchDisp": "NASDAQ",
            "quoteType": "EQUITY"}
    base.update(kw)
    return base


def test_maps_yahoo_quotes_to_hits():
    fake = MagicMock()
    fake.quotes = [
        _quote(),
        _quote(symbol="RELIANCE.NS", shortname=None, longname="Reliance Industries",
               exchDisp="NSE"),
        {"shortname": "no symbol — dropped"},
        _quote(symbol="MMYT260918C00060000", quoteType="OPTION"),
    ]
    with patch("yfinance.Search", return_value=fake):
        out = search_tickers("apple")
    assert out == {
        "results": [
            {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ",
             "type": "EQUITY"},
            {"symbol": "RELIANCE.NS", "name": "Reliance Industries",
             "exchange": "NSE", "type": "EQUITY"},
        ]
    }


def test_short_query_returns_empty_without_calling_yahoo():
    with patch("yfinance.Search") as m:
        assert search_tickers(" a ") == {"results": []}
    m.assert_not_called()


def test_yahoo_failure_degrades_to_empty():
    with patch("yfinance.Search", side_effect=RuntimeError("yahoo down")):
        assert search_tickers("apple") == {"results": []}
