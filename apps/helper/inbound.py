"""Chat Completions request -> NormalizedRequest.

Runs once, provider-independently. Everything here is about being faithful to
what the caller actually sent, because the adapter downstream can only make
correct decisions about a parameter it can see.

Three fidelity rules learned the hard way (plan §0.2):

* ``tool_choice`` is *absent* from langchain's structured-output binding, not
  ``null``. Synthesizing ``"auto"`` would send a field the caller never set.
* ``strict`` is likewise absent. Do not default it to False.
* ``parallel_tool_calls`` IS sent as ``False`` on that path
  (``langchain_openai`` sets it in ``bind_kwargs``), so it must survive parsing.
"""
from __future__ import annotations

from typing import Any

from apps.helper.types import BadRequest, Msg, NormalizedRequest, ToolCall, ToolDef

_KNOWN_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}

# Parsed explicitly; anything else lands in `extra` for diagnostics.
_HANDLED_KEYS = {
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "reasoning_effort", "stream",
}


def _content_to_text(content: Any) -> str | None:
    """Flatten Chat Completions content to text, preserving None vs ""."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal parts. Text-only for now; a non-text part is a hard error
        # rather than a silent drop, because silently losing an image would
        # produce a confidently wrong answer.
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype in ("text", "input_text"):
                parts.append(p.get("text") or "")
            else:
                raise BadRequest(
                    f"unsupported content part type {ptype!r}; this helper is text-only"
                )
        return "".join(parts)
    raise BadRequest(f"unsupported content type {type(content).__name__}")


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    out: list[ToolCall] = []
    for tc in raw or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        call_id = tc.get("id")
        name = fn.get("name")
        if not call_id or not name:
            raise BadRequest("tool_calls entries require both 'id' and 'function.name'")
        out.append(
            ToolCall(
                id=str(call_id),
                name=str(name),
                # Kept as the raw string. Round-tripping through json would
                # reorder keys and change whitespace the model chose.
                arguments=fn.get("arguments") or "{}",
            )
        )
    return out


def _parse_messages(raw: Any) -> list[Msg]:
    if not isinstance(raw, list) or not raw:
        raise BadRequest("'messages' must be a non-empty array")
    msgs: list[Msg] = []
    for m in raw:
        if not isinstance(m, dict):
            raise BadRequest("each message must be an object")
        role = m.get("role")
        if role not in _KNOWN_ROLES:
            raise BadRequest(f"unknown message role {role!r}")
        msg = Msg(
            role=role,
            content=_content_to_text(m.get("content")),
            name=m.get("name"),
            tool_calls=_parse_tool_calls(m.get("tool_calls")),
            tool_call_id=m.get("tool_call_id"),
        )
        if msg.role == "tool" and not msg.tool_call_id:
            raise BadRequest("role='tool' messages require 'tool_call_id'")
        msgs.append(msg)
    return msgs


def _parse_tools(raw: Any) -> list[ToolDef]:
    tools: list[ToolDef] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") not in (None, "function"):
            raise BadRequest(f"unsupported tool type {t.get('type')!r}")
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            raise BadRequest("tool entries require 'function.name'")
        tools.append(
            ToolDef(
                name=str(name),
                description=fn.get("description"),
                parameters=fn.get("parameters") or {"type": "object", "properties": {}},
                # `.get` not `.get(..., False)` — absent must stay absent.
                strict=fn.get("strict"),
            )
        )
    return tools


def _validate_tool_choice(choice: Any, tools: list[ToolDef]) -> Any:
    """Reject tool_choice shapes we cannot honour, and unknown named tools."""
    if choice is None:
        return None
    if isinstance(choice, str):
        if choice not in ("auto", "none", "required"):
            raise BadRequest(f"unsupported tool_choice {choice!r}")
        return choice
    if isinstance(choice, dict):
        if choice.get("type") != "function":
            raise BadRequest(f"unsupported tool_choice type {choice.get('type')!r}")
        wanted = (choice.get("function") or {}).get("name")
        if not wanted:
            raise BadRequest("named tool_choice requires function.name")
        if tools and wanted not in {t.name for t in tools}:
            raise BadRequest(
                f"tool_choice names {wanted!r}, which is not in 'tools'"
            )
        return choice
    raise BadRequest("tool_choice must be a string or an object")


def parse_chat_completions(body: dict[str, Any]) -> NormalizedRequest:
    """Parse a Chat Completions body. Raises ``BadRequest`` on malformed input."""
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    model = body.get("model")
    if not model or not isinstance(model, str):
        raise BadRequest("'model' is required")

    tools = _parse_tools(body.get("tools"))
    # max_completion_tokens is the newer spelling; both mean the same thing to us
    # and both are stripped for Codex (M0: max_output_tokens is rejected).
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))

    return NormalizedRequest(
        model=model,
        messages=_parse_messages(body.get("messages")),
        tools=tools,
        tool_choice=_validate_tool_choice(body.get("tool_choice"), tools),
        parallel_tool_calls=body.get("parallel_tool_calls"),
        response_format=body.get("response_format"),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        max_tokens=max_tokens,
        reasoning_effort=body.get("reasoning_effort"),
        stream=bool(body.get("stream", False)),
        extra={k: v for k, v in body.items() if k not in _HANDLED_KEYS},
    )
