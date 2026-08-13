"""Scanner definition AST — the one schema shared by builder UI, NL generator,
prebuilt seeds, storage, and the engine."""
from __future__ import annotations

import json
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMEFRAMES = ("5m", "15m", "1h", "1d", "1w", "1mo")
Timeframe = Literal["5m", "15m", "1h", "1d", "1w", "1mo"]

FIELDS = {"open", "high", "low", "close", "volume", "vwap", "typical_price",
          "gap_pct", "change_pct", "body", "upper_wick", "lower_wick"}
FUNCTIONS = {"SMA", "EMA", "WMA", "HMA", "VWMA", "RSI", "STOCH", "STOCHRSI", "CCI",
             "WILLR", "ROC", "MOM", "MACD", "ADX", "SUPERTREND", "PSAR", "ATR",
             "BBANDS", "BBWIDTH", "STDDEV", "OBV", "MFI", "CMF",
             "HIGHEST", "LOWEST", "SUM", "AVG", "COUNT"}
#: Functions that read OHLCV directly and ignore `of`.
OHLC_FUNCTIONS = {"STOCH", "CCI", "WILLR", "ADX", "SUPERTREND", "PSAR", "ATR",
                  "OBV", "MFI", "CMF"}
#: Functions with defaulted params (no `period` required).
NO_PERIOD_OK = {"MACD", "PSAR", "OBV", "SUPERTREND"}
PATTERNS = {"doji", "hammer", "inverted_hammer", "shooting_star", "hanging_man",
            "bullish_engulfing", "bearish_engulfing", "morning_star", "evening_star",
            "three_white_soldiers", "three_black_crows", "piercing", "dark_cloud_cover"}
FUNDAMENTALS = {"market_cap", "pe", "pb", "roe", "dividend_yield", "eps",
                "debt_to_equity", "revenue_growth"}
METAS = {"sector", "industry", "index", "fno"}
EXPR_OPS = {"+", "-", "*", "/", "abs", "min", "max"}

MAX_PERIOD = 500
MAX_NODES = 50
MAX_DEPTH = 8
MAX_JSON_BYTES = 32768


class DefinitionError(ValueError):
    """Definition breaks a hard limit (size / node count / depth)."""


class Operand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    const: Optional[float] = None
    const_str: Optional[str] = None
    const_list: Optional[List[str]] = None
    field: Optional[str] = None
    fn: Optional[str] = None
    of: Optional[Union[str, "Operand"]] = None   # base series for fn (default close)
    period: Optional[int] = Field(None, ge=1, le=MAX_PERIOD)
    params: Dict[str, float] = Field(default_factory=dict)  # e.g. MACD fast/slow/signal, BBANDS std
    component: Optional[str] = None              # MACD line|signal|hist, BBANDS upper|mid|lower, STOCH k|d, ADX adx|pdi|mdi
    expr: Optional[str] = None
    args: Optional[List["Operand"]] = None
    fundamental: Optional[str] = None
    meta: Optional[str] = None
    pattern: Optional[str] = None
    cond: Optional["Condition"] = None           # COUNT only
    bars_ago: int = Field(0, ge=0, le=MAX_PERIOD)

    @model_validator(mode="after")
    def _check(self) -> "Operand":
        kinds = [k for k in ("const", "const_str", "const_list", "field", "fn",
                             "expr", "fundamental", "meta", "pattern")
                 if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"operand must set exactly one kind, got {kinds or 'none'}")
        if self.field is not None and self.field not in FIELDS:
            raise ValueError(f"unknown field {self.field!r}")
        if self.fn is not None:
            if self.fn not in FUNCTIONS:
                raise ValueError(f"unknown function {self.fn!r}")
            if self.fn == "COUNT":
                if self.cond is None or self.period is None:
                    raise ValueError("COUNT needs cond and period")
            elif self.period is None and self.fn not in NO_PERIOD_OK:
                raise ValueError(f"{self.fn} needs period")
            if isinstance(self.of, str) and self.of not in FIELDS:
                raise ValueError(f"unknown base field {self.of!r}")
        if self.expr is not None and (self.expr not in EXPR_OPS or not self.args):
            raise ValueError("expr needs a known operator and args")
        if self.fundamental is not None and self.fundamental not in FUNDAMENTALS:
            raise ValueError(f"unknown fundamental {self.fundamental!r}")
        if self.meta is not None and self.meta not in METAS:
            raise ValueError(f"unknown meta {self.meta!r}")
        if self.pattern is not None and self.pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {self.pattern!r}")
        return self


Op = Literal[">", "<", ">=", "<=", "==", "!=", "in", "crosses_above", "crosses_below"]


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeframe: Timeframe = "1d"
    left: Operand
    op: Optional[Op] = None
    right: Optional[Operand] = None
    for_n_bars: Optional[int] = Field(None, ge=1, le=100)

    @model_validator(mode="after")
    def _shape(self) -> "Condition":
        if self.left.pattern is not None:
            if self.op is not None or self.right is not None:
                raise ValueError("pattern condition takes no op/right")
        elif self.op is None or self.right is None:
            raise ValueError("condition needs op and right")
        else:
            # Cross-validation of op vs operand kinds (when op and right are present)
            if self.right.pattern is not None:
                raise ValueError("pattern operand only allowed as a bare left condition")
            if self.op == "in":
                if self.right.const_list is None:
                    raise ValueError("'in' requires a const_list right operand")
            else:
                if self.right.const_list is not None:
                    raise ValueError("const_list is only valid with the 'in' operator")
            if self.op in ("crosses_above", "crosses_below"):
                if (self.left.meta is not None or self.left.const_str is not None or
                    self.right.meta is not None or self.right.const_str is not None):
                    raise ValueError("crosses require numeric operands")
            if self.left.meta is not None:
                if self.op not in ("==", "!=", "in"):
                    raise ValueError("meta conditions only support ==, !=, in")
        return self


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logic: Literal["AND", "OR"]
    children: List[Union["Group", Condition]] = Field(min_length=1)


Operand.model_rebuild()
Condition.model_rebuild()
Group.model_rebuild()

DEFINITION_JSON_SCHEMA = Group.model_json_schema()


def _walk(node: Union[Group, Condition], depth: int) -> tuple[int, int]:
    """(condition count, max depth) under node."""
    if isinstance(node, Condition):
        return 1, depth
    total, deepest = 0, depth
    for child in node.children:
        n, d = _walk(child, depth + 1)
        total += n
        deepest = max(deepest, d)
    return total, deepest


def parse_definition(data: dict) -> Group:
    raw = json.dumps(data)
    if len(raw.encode()) > MAX_JSON_BYTES:
        raise DefinitionError(f"definition exceeds {MAX_JSON_BYTES} bytes")
    group = Group.model_validate(data)
    nodes, depth = _walk(group, 1)
    if nodes > MAX_NODES:
        raise DefinitionError(f"too many conditions ({nodes} > {MAX_NODES})")
    if depth > MAX_DEPTH:
        raise DefinitionError(f"nesting too deep ({depth} > {MAX_DEPTH})")
    return group
