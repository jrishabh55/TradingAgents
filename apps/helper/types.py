"""Provider-neutral request/response shapes.

Inbound is always OpenAI Chat Completions — that is what langchain sends to a
custom ``base_url`` and it never varies. It is parsed ONCE into these types, and
adapters translate from here. Without that single normalization step every new
provider would re-implement Chat Completions parsing, which is exactly the
rework this design exists to avoid.

Deliberately absent: any ``opaque`` passthrough. M0 measured the live Codex
endpoint returning no ``reasoning`` items at all and a two-turn tool loop
succeeding with history replayed verbatim, so no provider-private channel is
needed (plan §8.1). If a future provider needs one, it belongs on that adapter's
own state, not in this shared vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Role = Literal["system", "developer", "user", "assistant", "tool", "function"]


@dataclass
class ToolCall:
    """A function call requested by the model."""

    id: str
    name: str
    # Raw JSON string exactly as the model emitted it. Never re-serialized:
    # callers compare arguments byte-for-byte in some flows.
    arguments: str


@dataclass
class Msg:
    """One conversation message.

    ``content is None`` and ``content == ""`` are different on the wire — an
    assistant message carrying only tool calls has null content — so the
    distinction is preserved rather than normalized away.
    """

    role: Role
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on role="tool" messages; correlates with ToolCall.id.
    tool_call_id: Optional[str] = None


@dataclass
class ToolDef:
    """A function tool the model may call."""

    name: str
    description: Optional[str]
    parameters: dict[str, Any]
    # None means the caller did not send `strict`. langchain omits it entirely
    # for the function-calling structured-output path, so we must not invent it.
    strict: Optional[bool] = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class NormalizedRequest:
    """A parsed Chat Completions request, provider-neutral.

    Every semantically meaningful parameter is carried even when the current
    upstream rejects it. Dropping a parameter at parse time would make it
    invisible to the adapter, so the adapter could not choose to strip it
    deliberately — and a silently dropped `temperature` reads as a helper bug.
    """

    model: str
    messages: list[Msg]
    tools: list[ToolDef] = field(default_factory=list)
    # Verbatim Chat Completions shape: "auto" | "none" | "required" |
    # {"type": "function", "function": {"name": ...}} | None (absent).
    tool_choice: Any = None
    # None means absent. langchain sends False on the structured-output path.
    parallel_tool_calls: Optional[bool] = None
    response_format: Optional[dict[str, Any]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    stream: bool = False
    # Anything else the caller sent, kept for diagnostics only. Adapters must
    # not read this — it exists so `strip_params` can log what it discarded.
    extra: dict[str, Any] = field(default_factory=dict)


FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]


@dataclass
class NormalizedResponse:
    """A provider reply, ready to render as Chat Completions."""

    model: str
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = "stop"
    usage: Usage = field(default_factory=Usage)
    # Populated when the model declined; rendered as content per Chat
    # Completions, since that API has no dedicated refusal field on the
    # non-streaming shape we emit.
    refusal: Optional[str] = None


class HelperError(Exception):
    """Base for errors that map to a specific HTTP status.

    Carries an OpenAI-shaped error envelope so callers (including langchain's
    own error handling) see something they already know how to read.
    """

    status: int = 500
    code: str = "helper_error"

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.retry_after = retry_after

    def envelope(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.code, "code": self.code}}


class BadRequest(HelperError):
    status = 400
    code = "invalid_request_error"


class Unauthorized(HelperError):
    status = 401
    code = "authentication_error"


class RateLimited(HelperError):
    status = 429
    code = "rate_limit_error"


class UpstreamFailure(HelperError):
    status = 502
    code = "upstream_error"
