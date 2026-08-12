"""Codex Responses adapter — ChatGPT-subscription-backed inference.

Wire contract measured against the live endpoint in M0, not inferred:

* ``POST https://chatgpt.com/backend-api/codex/responses``
* ``store: false`` and ``stream: true`` are mandatory
* ``max_output_tokens`` is rejected outright (400 Unsupported parameter), so
  inbound ``max_tokens`` must be stripped, never translated
* accepted models: sol / terra / luna / gpt-5.5 / gpt-5.4. ``gpt-5``,
  ``gpt-5-codex`` and ``codex-mini-latest`` are rejected — the third-party
  prior art's model list is stale
* custom function tools work; strict ``json_schema`` structured output works
* **no** ``reasoning`` items come back, and a two-turn tool loop succeeds with
  history replayed verbatim, so the adapter is stateless
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Optional

from apps.helper.quirks import ModelAlias, ProviderQuirks
from apps.helper.types import (
    BadRequest,
    FinishReason,
    Msg,
    NormalizedRequest,
    NormalizedResponse,
    RateLimited,
    ToolCall,
    Unauthorized,
    UpstreamFailure,
    Usage,
)

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"

_REAL_MODELS = frozenset(
    {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"}
)

# Aliases exist because per-tier reasoning effort cannot be expressed through
# this repo's config — one kwargs dict feeds both the deep and quick clients.
# The pipeline DOES set deep_think_llm and quick_think_llm independently, so the
# tier rides in the model name. Point quick at luna and deep at sol and the ten
# legwork agents stop consuming frontier quota.
# NOTE: alias names must NOT contain the substring "codex".
# langchain_openai's `_model_prefers_responses_api()` returns True for any model
# name containing "codex", which silently switches the client onto the Responses
# API and makes it POST /responses instead of /chat/completions — a 404 against
# this helper. Cost an hour to find; there is a test pinning it.
CODEX_ALIASES: dict[str, ModelAlias] = {
    "ta-quick": ModelAlias("gpt-5.6-luna", "low"),
    "ta-deep": ModelAlias("gpt-5.6-sol", "high"),
    **{
        f"{m}-{eff}": ModelAlias(m, eff)
        for m in _REAL_MODELS
        for eff in ("none", "low", "medium", "high")
    },
}

CODEX_QUIRKS = ProviderQuirks(
    valid_models=_REAL_MODELS,
    aliases=CODEX_ALIASES,
    mandatory_body={"store": False, "stream": True},
    # temperature/top_p: reasoning models reject them, and _get_provider_kwargs
    # forwards temperature whenever config sets it.
    # max_tokens: measured 400 in M0.
    strip_params=frozenset({"temperature", "top_p", "max_tokens"}),
    headers={"originator": "codex_cli_rs", "Accept": "text/event-stream"},
    json_schema_style="responses_text_format",
    default_reasoning_effort="medium",
)


# --------------------------------------------------------------------------
# request translation
# --------------------------------------------------------------------------


def _instructions_and_input(messages: list[Msg]) -> tuple[str, list[dict[str, Any]]]:
    """Split messages into top-level ``instructions`` plus the ``input`` array.

    Only a LEADING run of system/developer messages is hoisted. Codex requires
    ``instructions`` to be populated, but hoisting a *late* system message would
    silently promote it above everything before it and change its meaning — so
    later ones stay inline as input messages, preserving order and precedence.
    """
    instructions_parts: list[str] = []
    idx = 0
    while idx < len(messages) and messages[idx].role in ("system", "developer"):
        if messages[idx].content:
            instructions_parts.append(messages[idx].content)
        idx += 1

    items: list[dict[str, Any]] = []
    for m in messages[idx:]:
        items.extend(_message_to_items(m))
    return "\n\n".join(instructions_parts), items


def _message_to_items(m: Msg) -> list[dict[str, Any]]:
    """One Chat Completions message -> zero or more Responses input items."""
    if m.role == "tool" or m.role == "function":
        return [
            {
                "type": "function_call_output",
                "call_id": m.tool_call_id or "",
                "output": m.content or "",
            }
        ]

    items: list[dict[str, Any]] = []

    if m.role == "assistant":
        if m.content:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": m.content}],
                }
            )
        # Assistant tool calls replay as function_call items. Verified in M0:
        # replaying [user, function_call, function_call_output] works.
        for tc in m.tool_calls:
            items.append(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
            )
        return items

    # system/developer appearing after the leading run, plus all user messages.
    role = "user" if m.role in ("user",) else m.role
    return [
        {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": m.content or ""}],
        }
    ]


def _translate_tool_choice(choice: Any) -> Any:
    """Chat Completions tool_choice -> Responses tool_choice.

    Absent stays absent: langchain omits tool_choice on the structured-output
    path, and inventing ``"auto"`` would send a field the caller never set.
    """
    if choice is None:
        return None
    if choice in ("auto", "none"):
        return choice
    if choice == "required":
        return "required"
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        return {"type": "function", "name": name}
    raise BadRequest(f"cannot translate tool_choice {choice!r}")


def build_request_body(req: NormalizedRequest, quirks: ProviderQuirks) -> dict[str, Any]:
    """NormalizedRequest -> the Codex Responses body."""
    real_model, effort = quirks.resolve_model(req.model)
    instructions, input_items = _instructions_and_input(req.messages)

    body: dict[str, Any] = {
        "model": real_model,
        "instructions": instructions,
        "input": input_items,
    }

    if req.tools:
        body["tools"] = [
            # strict is only sent when the caller sent it.
            {
                k: v
                for k, v in {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": t.strict,
                }.items()
                if v is not None
            }
            for t in req.tools
        ]

    tool_choice = _translate_tool_choice(req.tool_choice)
    if tool_choice is not None:
        body["tool_choice"] = tool_choice

    if req.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = req.parallel_tool_calls

    chosen_effort = req.reasoning_effort or effort
    if chosen_effort:
        body["reasoning"] = {"effort": chosen_effort}

    if quirks.json_schema_style == "responses_text_format" and req.response_format:
        fmt = req.response_format
        if fmt.get("type") == "json_schema":
            js = fmt.get("json_schema") or {}
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": js.get("name", "response"),
                    "strict": bool(js.get("strict", True)),
                    "schema": js.get("schema") or {},
                }
            }

    # Mandatory fields win over anything the caller sent.
    body.update(quirks.mandatory_body)
    return body


# --------------------------------------------------------------------------
# SSE assembly
# --------------------------------------------------------------------------

# Terminal statuses that are NOT success. Treating an aborted stream as success
# would hand the pipeline a silently truncated report.
_FAILED_EVENTS = {"response.failed", "response.incomplete", "error"}


class _CallSlot:
    """Accumulates one function call's arguments."""

    __slots__ = ("call_id", "name", "chunks")

    def __init__(self, call_id: str, name: str) -> None:
        self.call_id = call_id
        self.name = name
        self.chunks: list[str] = []

    def arguments(self) -> str:
        return "".join(self.chunks) or "{}"


def assemble_response(events: Iterable[dict[str, Any]], *, model: str) -> NormalizedResponse:
    """Fold a Codex SSE event stream into a NormalizedResponse.

    Slots are keyed by ``(output_index, item_id)`` rather than ``call_id``:
    argument deltas identify their item by index/id, and ``call_id`` is not
    reliably present on every delta. Keying on call_id alone cannot safely
    assemble parallel tool calls.
    """
    text_parts: list[str] = []
    refusal_parts: list[str] = []
    slots: dict[tuple[Any, Any], _CallSlot] = {}
    order: list[tuple[Any, Any]] = []
    usage = Usage()
    saw_terminal = False
    incomplete_reason: Optional[str] = None

    for evt in events:
        etype = evt.get("type") or ""

        if etype in _FAILED_EVENTS:
            detail = evt.get("response") or evt
            msg = _extract_error_message(detail)
            if etype == "response.incomplete":
                incomplete_reason = msg or "incomplete"
                saw_terminal = True
                continue
            _raise_for_error(msg, detail)

        elif etype == "response.output_item.added":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                key = (evt.get("output_index"), item.get("id"))
                slots[key] = _CallSlot(item.get("call_id") or "", item.get("name") or "")
                order.append(key)

        elif etype == "response.function_call_arguments.delta":
            key = _slot_key(evt, slots)
            if key is not None:
                slots[key].chunks.append(evt.get("delta") or "")

        elif etype == "response.function_call_arguments.done":
            key = _slot_key(evt, slots)
            if key is not None and evt.get("arguments"):
                # Terminal value wins over accumulated deltas when both exist.
                slots[key].chunks = [evt["arguments"]]

        elif etype == "response.output_item.done":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                key = (evt.get("output_index"), item.get("id"))
                if key not in slots:
                    slots[key] = _CallSlot(
                        item.get("call_id") or "", item.get("name") or ""
                    )
                    order.append(key)
                slot = slots[key]
                slot.call_id = item.get("call_id") or slot.call_id
                slot.name = item.get("name") or slot.name
                if item.get("arguments"):
                    slot.chunks = [item["arguments"]]

        elif etype == "response.output_text.delta":
            text_parts.append(evt.get("delta") or "")

        elif etype == "response.refusal.delta":
            refusal_parts.append(evt.get("delta") or "")

        elif etype == "response.completed":
            saw_terminal = True
            resp = evt.get("response") or {}
            u = resp.get("usage") or {}
            usage = Usage(
                prompt_tokens=int(u.get("input_tokens") or 0),
                completion_tokens=int(u.get("output_tokens") or 0),
                reasoning_tokens=int(
                    (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
                ),
            )

    if not saw_terminal:
        # A truncated stream is indistinguishable from a short answer unless we
        # insist on a terminal event. Silence here would ship partial reports.
        raise UpstreamFailure("upstream stream ended without a terminal event")

    tool_calls = [
        ToolCall(id=slots[k].call_id, name=slots[k].name, arguments=slots[k].arguments())
        for k in order
        if slots[k].call_id and slots[k].name
    ]

    finish: FinishReason = "tool_calls" if tool_calls else "stop"
    if incomplete_reason and not tool_calls:
        finish = "length"

    return NormalizedResponse(
        model=model,
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=usage,
        refusal="".join(refusal_parts) or None,
    )


def _slot_key(evt: dict[str, Any], slots: dict[tuple[Any, Any], _CallSlot]):
    """Best-effort slot lookup for a delta event."""
    key = (evt.get("output_index"), evt.get("item_id"))
    if key in slots:
        return key
    # Fall back to matching on item_id alone — output_index is absent on some
    # delta shapes.
    for k in slots:
        if k[1] is not None and k[1] == evt.get("item_id"):
            return k
    # Single in-flight call: unambiguous.
    if len(slots) == 1:
        return next(iter(slots))
    return None


def _extract_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        for key in ("detail", "message"):
            if isinstance(detail.get(key), str):
                return detail[key]
        err = detail.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        inc = detail.get("incomplete_details")
        if isinstance(inc, dict) and isinstance(inc.get("reason"), str):
            return inc["reason"]
    return ""


def _raise_for_error(message: str, detail: Any) -> None:
    """Map an in-stream error to the right HTTP status.

    Rate-limit and auth failures can arrive inside an HTTP 200 SSE stream, so
    collapsing everything to 502 would destroy Retry-After semantics and make a
    dead session look like a transient upstream blip.
    """
    lowered = (message or "").lower()
    code = ""
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
    haystack = f"{lowered} {code.lower()}"

    if "rate" in haystack and "limit" in haystack:
        retry = None
        if isinstance(detail, dict):
            retry = (detail.get("error") or {}).get("retry_after") if isinstance(
                detail.get("error"), dict
            ) else None
        raise RateLimited(message or "rate limited upstream", retry_after=retry)
    if any(t in haystack for t in ("unauthor", "authentication", "invalid_token", "401")):
        raise Unauthorized(message or "upstream rejected the credential")
    raise UpstreamFailure(message or "upstream reported a failure")


def new_session_id() -> str:
    """Dynamic per-request header. Static headers live in quirks."""
    return str(uuid.uuid4())


def parse_sse_lines(lines: Iterable[bytes | str]) -> Iterable[dict[str, Any]]:
    """Yield decoded JSON objects from an SSE byte/line stream."""
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class CodexResponsesAdapter:
    """Sends a NormalizedRequest to the Codex Responses endpoint.

    Streaming is mandatory upstream (`stream: true`), so this always consumes an
    SSE stream even though it returns a single non-streaming result. The stream is
    abandoned as soon as the caller cancels, so a cancelled run stops billing
    instead of running to completion in the background.
    """

    name = "codex-responses"

    def __init__(self, url: str = CODEX_URL, client: Any = None) -> None:
        self._url = url
        self._client = client  # injectable for tests

    async def send(self, req, cred, quirks, ctx):  # noqa: ANN001 - Protocol-typed
        import httpx

        body = build_request_body(req, quirks)
        headers = {
            "Authorization": f"Bearer {cred.token}",
            "Content-Type": "application/json",
            "session_id": new_session_id(),  # dynamic; static ones are in quirks
            **quirks.headers,
            **cred.headers,
        }

        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(ctx.timeout_s))
        owns_client = self._client is None
        try:
            async with client.stream(
                "POST", self._url, json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    _raise_for_status(response.status_code, raw, response.headers)
                events = []
                async for line in response.aiter_lines():
                    if ctx.is_cancelled():
                        # Leaving the context manager aborts the HTTP stream.
                        raise UpstreamFailure("call cancelled by caller")
                    stripped = line.strip()
                    if not stripped.startswith("data:"):
                        continue
                    payload = stripped[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        continue
        finally:
            if owns_client:
                await client.aclose()

        real_model, _ = quirks.resolve_model(req.model)
        return assemble_response(events, model=real_model)


def _raise_for_status(status: int, raw: str, headers: Any) -> None:
    """Map an upstream HTTP error to the right status, preserving what we can."""
    message = raw
    try:
        parsed = json.loads(raw)
        message = _extract_error_message(parsed) or raw
    except json.JSONDecodeError:
        pass

    if status == 429:
        retry = None
        try:
            retry = headers.get("retry-after")
        except Exception:  # noqa: BLE001
            pass
        raise RateLimited(message or "rate limited upstream", retry_after=retry)
    if status in (401, 403):
        raise Unauthorized(message or "upstream rejected the credential")
    if status == 400:
        # Not every 400 is a model error — surface the upstream text so a
        # rejected parameter is diagnosable rather than a generic failure.
        raise BadRequest(message or "upstream rejected the request")
    raise UpstreamFailure(f"upstream returned {status}: {message[:300]}")
