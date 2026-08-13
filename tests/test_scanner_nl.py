from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apps.api.scanner import nl

GOOD = json.dumps({
    "explanation": "Close above 200 SMA with RSI over 60.",
    "definition": {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
         "right": {"fn": "SMA", "of": "close", "period": 200}},
        {"timeframe": "1d", "left": {"fn": "RSI", "of": "close", "period": 14},
         "op": ">", "right": {"const": 60}}]},
})

BAD = json.dumps({"explanation": "nope", "definition": {
    "logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "closse"}, "op": ">", "right": {"const": 1}}]}})


def test_valid_output_passes_through():
    with patch.object(nl, "_invoke", return_value=GOOD):
        definition, explanation = nl.generate_definition("momentum stocks")
    assert definition["children"][0]["right"]["period"] == 200
    assert "RSI" in explanation


def test_invalid_then_valid_retries_once():
    with patch.object(nl, "_invoke", side_effect=[BAD, GOOD]) as mock:
        definition, _ = nl.generate_definition("momentum stocks")
    assert mock.call_count == 2
    assert definition["logic"] == "AND"
    # Verify retry mechanics: second call includes error message
    second_call_messages = mock.call_args_list[1][0][0]
    assert any("That definition was invalid" in msg[1] for msg in second_call_messages if msg[0] == "human")
    # Verify AI message with BAD output was appended
    assert any(msg == ("ai", BAD) for msg in second_call_messages)


def test_two_failures_raise():
    with patch.object(nl, "_invoke", side_effect=[BAD, BAD, BAD]) as mock:
        with pytest.raises(nl.NlGenerationError):
            nl.generate_definition("momentum stocks")
    # Verify attempt cap: only 2 calls made, third element unconsumed
    assert mock.call_count == 2


def test_json_extracted_from_fenced_output():
    fenced = f"Here you go:\n```json\n{GOOD}\n```"
    with patch.object(nl, "_invoke", return_value=fenced):
        definition, _ = nl.generate_definition("x")
    assert definition["logic"] == "AND"


def test_prose_wrapped_json_with_extra_braces():
    # Regression test: prose with braces around the JSON must extract correctly
    prose_wrapped = f"Here {{is}} your scan:\n```json\n{GOOD}\n```\nEnjoy {{it}}!"
    with patch.object(nl, "_invoke", return_value=prose_wrapped):
        definition, _ = nl.generate_definition("x")
    assert definition["logic"] == "AND"
    assert definition["children"][0]["right"]["period"] == 200
