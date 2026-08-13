"""Vectorized candlestick pattern rules on wide OHLC frames.

Textbook geometric definitions. Trend-context patterns (shooting star,
hanging man, stars) use a 3-bars-back close comparison as the trend proxy —
ponytail: crude but cheap; swap for an SMA-slope filter if false positives bug users.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from apps.api.scanner.indicators import Panel


def pattern_frame(name: str, panel: Panel) -> pd.DataFrame:
    p = panel
    o, h, l, c = p.open, p.high, p.low, p.close
    body = c - o
    ab = body.abs()
    rng = (h - l).replace(0, np.nan)
    upsh = h - np.maximum(o, c)
    losh = np.minimum(o, c) - l
    green = c > o
    red = o > c
    o1, c1, ab1 = o.shift(1), c.shift(1), ab.shift(1)
    green1, red1 = green.shift(1, fill_value=False), red.shift(1, fill_value=False)
    uptrend = c.shift(1) > c.shift(3)
    downtrend = c.shift(1) < c.shift(3)

    if name == "doji":
        out = ab <= 0.1 * rng
    elif name == "hammer":
        out = (losh >= 2 * ab) & (upsh <= ab) & (ab > 0)
    elif name == "inverted_hammer":
        out = (upsh >= 2 * ab) & (losh <= ab) & (ab > 0)
    elif name == "shooting_star":
        out = (upsh >= 2 * ab) & (losh <= ab) & (ab > 0) & uptrend
    elif name == "hanging_man":
        out = (losh >= 2 * ab) & (upsh <= ab) & (ab > 0) & uptrend
    elif name == "bullish_engulfing":
        out = red1 & green & (o <= c1) & (c >= o1)
    elif name == "bearish_engulfing":
        out = green1 & red & (o >= c1) & (c <= o1)
    elif name == "morning_star":
        mid2 = (o.shift(2) + c.shift(2)) / 2
        out = (red.shift(2, fill_value=False) & (ab1 <= 0.3 * ab.shift(2))
               & green & (c > mid2) & downtrend.shift(1, fill_value=False))
    elif name == "evening_star":
        mid2 = (o.shift(2) + c.shift(2)) / 2
        out = (green.shift(2, fill_value=False) & (ab1 <= 0.3 * ab.shift(2))
               & red & (c < mid2) & uptrend.shift(1, fill_value=False))
    elif name == "three_white_soldiers":
        out = (green & green1 & green.shift(2, fill_value=False)
               & (c > c1) & (c1 > c.shift(2))
               & (o > o1) & (o < c1) & (o1 > o.shift(2)) & (o1 < c.shift(2)))
    elif name == "three_black_crows":
        out = (red & red1 & red.shift(2, fill_value=False)
               & (c < c1) & (c1 < c.shift(2))
               & (o < o1) & (o > c1) & (o1 < o.shift(2)) & (o1 > c.shift(2)))
    elif name == "piercing":
        out = red1 & green & (o < c1) & (c > (o1 + c1) / 2) & (c < o1)
    elif name == "dark_cloud_cover":
        out = green1 & red & (o > c1) & (c < (o1 + c1) / 2) & (c > o1)
    else:
        raise ValueError(f"unknown pattern {name}")
    return out.fillna(False)
