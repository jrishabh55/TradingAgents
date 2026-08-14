"""Natural language -> scanner definition via one LLM call (+1 retry).

Output is validated with the same parse_definition the API uses, so the model
cannot smuggle in anything the engine wouldn't accept. The result is shown in
the builder for user review — never auto-run.
"""
from __future__ import annotations

import json
import os

from apps.api.scanner.schema import (
    EXPR_OPS, FIELDS, FUNCTIONS, FUNDAMENTALS, METAS, PATTERNS, TIMEFRAMES,
    parse_definition,
)


class NlGenerationError(Exception):
    pass


_SYSTEM = f"""You convert plain-English stock screener descriptions into a JSON scanner definition.

Reply with ONLY a JSON object: {{"explanation": "<one sentence>", "definition": <Group>}}

A Group is {{"logic": "AND"|"OR", "children": [Group or Condition, ...]}}.
A Condition is {{"timeframe": tf, "left": Operand, "op": op, "right": Operand}} with optional "for_n_bars": n for streaks.
Pattern conditions are {{"timeframe": tf, "left": {{"pattern": name}}}} with no op/right.

Timeframes: {", ".join(TIMEFRAMES)} (default "1d").
Operators: > < >= <= == != in crosses_above crosses_below.
Operand kinds (exactly one per operand):
  {{"const": number}} | {{"const_str": "text"}} | {{"const_list": ["a","b"]}} (only valid as the right operand of 'in', paired with a "meta" left operand) | {{"field": name}} | {{"fundamental": name}} | {{"meta": name}} | {{"pattern": name}}
  {{"fn": NAME, "of": field, "period": n}} — optional "component" (MACD: line/signal/hist; BBANDS: upper/mid/lower; STOCH: k/d; ADX: adx/pdi/mdi), optional "params" (MACD fast/slow/signal, BBANDS std, SUPERTREND mult)
  {{"expr": "*", "args": [...]}} for arithmetic ({", ".join(sorted(EXPR_OPS))})
  Any operand may add "bars_ago": n.
Fields: {", ".join(sorted(FIELDS))}
Functions: {", ".join(sorted(FUNCTIONS))}
Patterns: {", ".join(sorted(PATTERNS))}
Fundamentals: {", ".join(sorted(FUNDAMENTALS))} (market_cap is in INR)
Meta: {", ".join(sorted(METAS))}

Period performance ("profitable/up this week/month") means the return over that period:
use one condition on the 1w/1mo timeframe, NOT a daily streak with for_n_bars.
Example: "profitable this week" ->
{{"timeframe": "1w", "left": {{"field": "close"}}, "op": ">", "right": {{"field": "open"}}}}

Example: "volume at least twice its 20 day average" ->
{{"timeframe": "1d", "left": {{"field": "volume"}}, "op": ">",
  "right": {{"expr": "*", "args": [{{"const": 2}}, {{"fn": "SMA", "of": "volume", "period": 20}}]}}}}"""


def _invoke(messages: list) -> str:
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=os.environ.get("SCANNER_NL_MODEL", "gpt-5.4-mini"),
                     temperature=0)
    return llm.invoke(messages).content


def _extract_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char == "{":
            try:
                obj, _ = decoder.raw_decode(text, idx)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object in model output")


def generate_definition(prompt: str) -> tuple[dict, str]:
    messages = [("system", _SYSTEM), ("human", prompt)]
    last_error = ""
    for attempt in range(2):
        if attempt:
            messages.append(("human",
                             f"That definition was invalid: {last_error}. "
                             "Reply again with ONLY the corrected JSON object."))
        raw = ""
        try:
            raw = _invoke(messages)
            payload = _extract_json(raw)
            parse_definition(payload["definition"])
            return payload["definition"], str(payload.get("explanation", ""))
        except Exception as exc:  # noqa: BLE001 — feed the error back for one retry
            last_error = str(exc)[:500]
            messages.append(("ai", raw or "(no output)"))
    raise NlGenerationError(f"could not generate a valid definition: {last_error}")
