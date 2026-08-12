"""Tests for the drishti-helper translation core (apps/helper/).

No network. The Codex wire contract these assert against was measured in M0 —
see docs/TA_HELPER_PLAN.md §0 and §8.1.
"""
import pytest

from apps.helper.inbound import parse_chat_completions
from apps.helper.outbound import render_chat_completion
from apps.helper.providers.codex import (
    CODEX_QUIRKS,
    assemble_response,
    build_request_body,
    parse_sse_lines,
)
from apps.helper.types import (
    BadRequest,
    NormalizedResponse,
    RateLimited,
    ToolCall,
    Unauthorized,
    UpstreamFailure,
    Usage,
)

TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock_data",
        "description": "latest close",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}},
    },
}


def _req(**over):
    body = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(over)
    return parse_chat_completions(body)


def _body(**over):
    return build_request_body(_req(**over), CODEX_QUIRKS)


# ---------- model resolution ----------


def test_no_alias_name_contains_codex():
    """langchain's `_model_prefers_responses_api` treats any model name
    containing "codex" as Responses-only, which would make it POST /responses
    and 404 against this helper."""
    from apps.helper.providers.codex import CODEX_ALIASES
    assert [a for a in CODEX_ALIASES if "codex" in a] == []


def test_quick_and_deep_aliases_map_to_model_plus_effort():
    """Per-tier effort can only ride in the model name — one kwargs dict feeds
    both the deep and quick clients, so config cannot express it."""
    q = build_request_body(_req(model="ta-quick"), CODEX_QUIRKS)
    d = build_request_body(_req(model="ta-deep"), CODEX_QUIRKS)
    assert (q["model"], q["reasoning"]["effort"]) == ("gpt-5.6-luna", "low")
    assert (d["model"], d["reasoning"]["effort"]) == ("gpt-5.6-sol", "high")


@pytest.mark.parametrize(
    "model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"]
)
def test_all_measured_models_accepted(model):
    assert _body(model=model)["model"] == model


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-codex", "codex-mini-latest"])
def test_models_measured_as_rejected_fail_with_a_useful_message(model):
    """gpt-5 died between the prior art being written and M0 — the error text
    is the documentation."""
    with pytest.raises(ValueError, match="Valid names"):
        _body(model=model)


# ---------- quirks as data ----------


def test_mandatory_fields_override_the_caller():
    b = _body(stream=False)
    assert b["store"] is False and b["stream"] is True


def test_rejected_parameters_are_stripped():
    """max_output_tokens is rejected outright upstream; temperature is rejected
    by reasoning models but _get_provider_kwargs forwards it whenever set."""
    b = _body(temperature=0.7, top_p=0.9, max_tokens=256)
    for gone in ("temperature", "top_p", "max_tokens", "max_output_tokens"):
        assert gone not in b


def test_max_completion_tokens_is_also_stripped():
    assert "max_tokens" not in _body(max_completion_tokens=99)


# ---------- fidelity: never invent a field the caller did not send ----------


def test_absent_tool_choice_stays_absent():
    assert "tool_choice" not in _body(tools=[TOOL])


def test_absent_strict_stays_absent():
    assert "strict" not in _body(tools=[TOOL])["tools"][0]


def test_strict_is_forwarded_when_the_caller_sends_it():
    t = {**TOOL, "function": {**TOOL["function"], "strict": True}}
    assert _body(tools=[t])["tools"][0]["strict"] is True


def test_parallel_tool_calls_false_survives():
    """langchain's function-calling structured-output path sets this in
    bind_kwargs, so it reaches the wire for all four schema-bound agents."""
    assert _body(tools=[TOOL], parallel_tool_calls=False)["parallel_tool_calls"] is False


def test_parallel_tool_calls_absent_stays_absent():
    assert "parallel_tool_calls" not in _body(tools=[TOOL])


@pytest.mark.parametrize("choice", ["auto", "none", "required"])
def test_string_tool_choices_translate(choice):
    assert _body(tools=[TOOL], tool_choice=choice)["tool_choice"] == choice


def test_named_tool_choice_translates_to_responses_shape():
    b = _body(
        tools=[TOOL],
        tool_choice={"type": "function", "function": {"name": "get_stock_data"}},
    )
    assert b["tool_choice"] == {"type": "function", "name": "get_stock_data"}


def test_named_tool_choice_for_unknown_tool_is_rejected():
    with pytest.raises(BadRequest, match="not in 'tools'"):
        _req(tools=[TOOL], tool_choice={"type": "function", "function": {"name": "nope"}})


def test_unsupported_tool_choice_string_is_rejected():
    with pytest.raises(BadRequest, match="unsupported tool_choice"):
        _req(tools=[TOOL], tool_choice="mandatory")


# ---------- instructions vs input ordering ----------


def test_leading_system_messages_hoist_to_instructions():
    b = _body(
        messages=[
            {"role": "system", "content": "A"},
            {"role": "developer", "content": "B"},
            {"role": "user", "content": "q"},
        ]
    )
    assert b["instructions"] == "A\n\nB"
    assert [i["type"] for i in b["input"]] == ["message"]


def test_a_late_system_message_stays_inline():
    """Hoisting it would silently promote it above everything before it."""
    b = _body(
        messages=[
            {"role": "system", "content": "lead"},
            {"role": "user", "content": "q"},
            {"role": "system", "content": "late"},
        ]
    )
    assert b["instructions"] == "lead"
    assert len(b["input"]) == 2
    assert b["input"][1]["role"] == "system"


# ---------- tool-loop replay (measured working in M0) ----------


def test_tool_loop_replays_as_function_call_and_output():
    b = _body(
        messages=[
            {"role": "user", "content": "AAPL?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_stock_data", "arguments": '{"symbol":"AAPL"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"close":308.91}'},
        ],
        tools=[TOOL],
    )
    kinds = [i["type"] for i in b["input"]]
    assert kinds == ["message", "function_call", "function_call_output"]
    assert b["input"][1]["call_id"] == "call_1"
    assert b["input"][1]["arguments"] == '{"symbol":"AAPL"}'
    assert b["input"][2]["call_id"] == "call_1"


def test_tool_arguments_are_not_reserialized():
    """Round-tripping through json would reorder keys and change whitespace."""
    raw = '{"b": 2,   "a":1}'
    b = _body(
        messages=[
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "get_stock_data", "arguments": raw}}
                ],
            },
        ],
        tools=[TOOL],
    )
    assert b["input"][1]["arguments"] == raw


def test_tool_message_without_id_is_rejected():
    with pytest.raises(BadRequest, match="tool_call_id"):
        _req(messages=[{"role": "tool", "content": "x"}])


def test_structured_output_response_format_becomes_text_format():
    b = _body(
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "r", "strict": True, "schema": {"type": "object"}},
        }
    )
    assert b["text"]["format"]["type"] == "json_schema"
    assert b["text"]["format"]["name"] == "r"


def test_multimodal_content_is_rejected_not_silently_dropped():
    with pytest.raises(BadRequest, match="text-only"):
        _req(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}])


# ---------- SSE assembly ----------


def _completed(inp=59, out=105, reasoning=63):
    return {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "output_tokens_details": {"reasoning_tokens": reasoning},
            }
        },
    }


def test_text_response_assembles_with_usage():
    r = assemble_response(
        [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "$308"},
            {"type": "response.output_text.delta", "delta": ".91"},
            _completed(),
        ],
        model="gpt-5.6-sol",
    )
    assert r.content == "$308.91"
    assert r.finish_reason == "stop"
    assert (r.usage.prompt_tokens, r.usage.completion_tokens, r.usage.reasoning_tokens) == (59, 105, 63)
    assert r.usage.total_tokens == 164


def test_single_tool_call_assembles():
    r = assemble_response(
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "i1", "type": "function_call", "call_id": "call_a", "name": "get_stock_data"},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "i1", "delta": '{"sym'},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "i1", "delta": 'bol":"AAPL"}'},
            {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "i1"},
            _completed(),
        ],
        model="m",
    )
    assert r.finish_reason == "tool_calls"
    assert [(t.id, t.name, t.arguments) for t in r.tool_calls] == [
        ("call_a", "get_stock_data", '{"symbol":"AAPL"}')
    ]


def test_parallel_tool_calls_are_kept_separate_and_ordered():
    """Slots key on (output_index, item_id) — keying on call_id alone cannot
    safely assemble parallel calls."""
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "i1", "type": "function_call", "call_id": "c1", "name": "a"}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"id": "i2", "type": "function_call", "call_id": "c2", "name": "b"}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "i2", "delta": '{"x":2}'},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "i1", "delta": '{"x":1}'},
        _completed(),
    ]
    r = assemble_response(events, model="m")
    assert [(t.id, t.arguments) for t in r.tool_calls] == [("c1", '{"x":1}'), ("c2", '{"x":2}')]


def test_terminal_arguments_win_over_accumulated_deltas():
    r = assemble_response(
        [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "i1", "type": "function_call", "call_id": "c", "name": "n"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "i1", "delta": "part"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"id": "i1", "type": "function_call", "call_id": "c", "name": "n", "arguments": '{"final":1}'}},
            _completed(),
        ],
        model="m",
    )
    assert r.tool_calls[0].arguments == '{"final":1}'


def test_a_stream_without_a_terminal_event_is_an_error():
    """A truncated stream would otherwise be indistinguishable from a short
    answer, and would ship a partial report as if complete."""
    with pytest.raises(UpstreamFailure, match="without a terminal event"):
        assemble_response(
            [{"type": "response.output_text.delta", "delta": "half"}], model="m"
        )


def test_response_failed_raises_upstream_failure():
    with pytest.raises(UpstreamFailure, match="boom"):
        assemble_response([{"type": "response.failed", "response": {"detail": "boom"}}], model="m")


def test_rate_limit_inside_a_200_stream_maps_to_429():
    """Collapsing this to 502 would destroy Retry-After semantics."""
    with pytest.raises(RateLimited):
        assemble_response(
            [{"type": "error", "error": {"code": "rate_limit_exceeded", "message": "Rate limit reached"}}],
            model="m",
        )


def test_auth_failure_inside_a_stream_maps_to_401():
    with pytest.raises(Unauthorized):
        assemble_response(
            [{"type": "error", "error": {"code": "invalid_token", "message": "unauthorized"}}],
            model="m",
        )


def test_incomplete_is_terminal_and_reports_length():
    r = assemble_response(
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
        ],
        model="m",
    )
    assert r.finish_reason == "length" and r.content == "partial"


def test_refusal_is_carried_through():
    r = assemble_response(
        [{"type": "response.refusal.delta", "delta": "I can't help"}, _completed()],
        model="m",
    )
    assert r.refusal == "I can't help"


def test_sse_line_parser_skips_noise_and_done():
    events = list(parse_sse_lines([b"", b": ping", b'data: {"type":"a"}', b"data: [DONE]", b"data: {bad"]))
    assert [e["type"] for e in events] == ["a"]


# ---------- renderer ----------


def test_tool_only_turn_renders_null_content_not_empty_string():
    """langchain distinguishes them; "" reads as "the model said nothing"."""
    out = render_chat_completion(
        NormalizedResponse(
            model="m",
            tool_calls=[ToolCall("c", "n", "{}")],
            finish_reason="tool_calls",
            usage=Usage(1, 2, 0),
        ),
        response_id="chatcmpl-x",
        created=1,
    )
    msg = out["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"][0]["function"]["name"] == "n"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["total_tokens"] == 3


def test_text_turn_renders_content_and_reasoning_token_detail():
    out = render_chat_completion(
        NormalizedResponse(model="m", content="hi", usage=Usage(5, 7, 3)),
        response_id="id",
        created=2,
    )
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
