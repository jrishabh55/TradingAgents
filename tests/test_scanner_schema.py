from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.scanner.schema import (
    DefinitionError, Group, MAX_NODES, parse_definition,
)

GOLDEN_CROSS = {"logic": "AND", "children": [
    {"timeframe": "1d",
     "left": {"fn": "SMA", "of": "close", "period": 50},
     "op": "crosses_above",
     "right": {"fn": "SMA", "of": "close", "period": 200}},
]}


def cond(**kw):
    base = {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}
    base.update(kw)
    return base


def test_valid_definition_parses():
    g = parse_definition(GOLDEN_CROSS)
    assert isinstance(g, Group)
    assert g.children[0].left.fn == "SMA"


def test_nested_groups_and_expr():
    d = {"logic": "OR", "children": [
        {"logic": "AND", "children": [
            cond(right={"expr": "*", "args": [{"const": 2},
                 {"fn": "SMA", "of": "volume", "period": 20}]}),
            {"timeframe": "1d", "left": {"pattern": "bullish_engulfing"}},
        ]},
        cond(left={"fundamental": "market_cap"}, right={"const": 1000}),
    ]}
    parse_definition(d)


def test_operand_must_have_exactly_one_kind():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"field": "close", "const": 5})]})
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [cond(left={})]})


def test_pattern_condition_takes_no_op():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"pattern": "doji"}, "op": ">", "right": {"const": 1}}]})


def test_unknown_names_rejected():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [cond(left={"field": "closse"})]})
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"fn": "SUPERDUPER", "period": 5})]})


def test_period_cap():
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            cond(left={"fn": "SMA", "of": "close", "period": 501})]})


def test_node_count_limit():
    d = {"logic": "AND", "children": [cond() for _ in range(MAX_NODES + 1)]}
    with pytest.raises(DefinitionError):
        parse_definition(d)


def test_depth_limit():
    d = cond()
    for _ in range(9):
        d = {"logic": "AND", "children": [d]}
    with pytest.raises(DefinitionError):
        parse_definition(d)


def test_size_limit():
    d = {"logic": "AND", "children": [cond() for _ in range(40)]}
    d["children"][0]["left"] = {"field": "close"}
    big = {"logic": "AND", "children": [dict(cond(), note="x" * 40000)]}
    with pytest.raises(DefinitionError):
        parse_definition(big)


def test_op_operand_cross_validation():
    # Pattern operand on right side should fail
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"pattern": "doji"}}]})

    # 'in' operator requires const_list on right
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"field": "close"}, "op": "in", "right": {"const": 100}}]})

    # const_list only valid with 'in' operator
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const_list": ["a", "b"]}}]})

    # crosses_above/crosses_below require numeric operands (no meta/const_str)
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"meta": "sector"}, "op": "crosses_above", "right": {"const": 100}}]})

    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"field": "close"}, "op": "crosses_above", "right": {"const_str": "bull"}}]})

    # meta conditions only support ==, !=, in
    with pytest.raises(ValidationError):
        parse_definition({"logic": "AND", "children": [
            {"timeframe": "1d", "left": {"meta": "sector"}, "op": ">", "right": {"const": 100}}]})

    # Valid meta 'in' condition should parse
    g = parse_definition({"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"meta": "sector"}, "op": "in", "right": {"const_list": ["IT", "Banking"]}}]})
    assert isinstance(g, Group)
    assert g.children[0].left.meta == "sector"
    assert g.children[0].op == "in"
    assert g.children[0].right.const_list == ["IT", "Banking"]
