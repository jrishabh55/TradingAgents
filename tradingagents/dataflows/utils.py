import re
from datetime import date, datetime, timedelta
from typing import Annotated

import pandas as pd

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


# Yahoo Finance exchange-suffix → broad-market index ticker.
# Used for the post-trade alpha calculation in the reflection memory log so
# that an Indian stock is benchmarked against the Nifty rather than the S&P.
_BENCHMARK_BY_SUFFIX = {
    ".NS": "^NSEI",     # NSE → Nifty 50
    ".BO": "^BSESN",    # BSE → Sensex
    ".TO": "^GSPTSE",   # TSX → S&P/TSX Composite
    ".T":  "^N225",     # Tokyo → Nikkei 225
    ".HK": "^HSI",      # Hong Kong → Hang Seng
    ".L":  "^FTSE",     # London → FTSE 100
    ".PA": "^FCHI",     # Paris → CAC 40
    ".DE": "^GDAXI",    # Frankfurt → DAX
    ".AX": "^AXJO",     # Australia → ASX 200
    ".SS": "000001.SS", # Shanghai → SSE Composite
    ".SZ": "399001.SZ", # Shenzhen → SZSE Component
}

DEFAULT_BENCHMARK = "SPY"


def benchmark_for(ticker: str) -> str:
    """Return the broad-market benchmark to compare ``ticker`` against.

    Maps Yahoo Finance exchange suffixes (``.NS``, ``.TO``, …) to the
    corresponding index symbol. Tickers without a known suffix fall back to
    SPY, which preserves prior behavior for US equities.
    """
    if not isinstance(ticker, str) or "." not in ticker:
        return DEFAULT_BENCHMARK
    suffix = "." + ticker.rsplit(".", 1)[1].upper()
    return _BENCHMARK_BY_SUFFIX.get(suffix, DEFAULT_BENCHMARK)


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date
