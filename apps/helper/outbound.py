"""NormalizedResponse -> Chat Completions response body.

Provider-independent. The only subtlety is `content`: an assistant message that
carries only tool calls must render `content: null`, not `""` — langchain
distinguishes them, and an empty string reads as "the model said nothing" rather
than "the model called a tool".
"""
from __future__ import annotations

from typing import Any

from apps.helper.types import NormalizedResponse


def render_chat_completion(
    resp: NormalizedResponse, *, response_id: str, created: int
) -> dict[str, Any]:
    """Build a non-streaming Chat Completions body.

    ``response_id`` and ``created`` are injected rather than generated here so
    the caller owns clock and id policy (and so tests are deterministic).
    """
    message: dict[str, Any] = {"role": "assistant"}

    if resp.refusal is not None:
        # Chat Completions has a `refusal` field on the message; populate both it
        # and content so callers that only read content still see something.
        message["refusal"] = resp.refusal
        message["content"] = resp.refusal
    else:
        message["content"] = resp.content

    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in resp.tool_calls
        ]
        # Explicit: tool-call-only turns carry null content.
        if not resp.content:
            message["content"] = None

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": resp.model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": resp.finish_reason}
        ],
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": resp.usage.reasoning_tokens
            },
        },
    }
