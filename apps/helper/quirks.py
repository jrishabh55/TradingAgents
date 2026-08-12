"""Per-provider deployment rules, as data.

Quirks live *beside* an adapter, never on it. One adapter serves several
providers that differ only by deployment — ``OpenAIChatCompletionsAdapter``
fronts native OpenAI, a local vLLM, and OpenRouter, which disagree about
supported parameters while speaking the identical protocol. Attaching these
fields to the adapter would make the second such provider a subclass, which is
the erosion this split exists to prevent.

So: `Provider = (name, adapter, credentials, quirks)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ModelAlias:
    """A caller-visible model name mapped to a real model plus a reasoning tier.

    Exists because per-tier reasoning effort cannot be expressed through this
    repo's config: ``TradingAgentsGraph`` builds ONE provider-kwargs dict and
    passes it to both the deep and quick clients, so the two tiers are
    indistinguishable downstream (Codex review finding #2). Encoding the tier in
    the model name is the only lever the pipeline already has — it sets
    ``deep_think_llm`` and ``quick_think_llm`` independently.
    """

    real_model: str
    reasoning_effort: Optional[str] = None


@dataclass(frozen=True)
class ProviderQuirks:
    """Deployment rules for one provider.

    Everything here is data so the adapter algorithm stays uniform. A new
    OpenAI-compatible deployment is a new quirks row, not a new code path.
    """

    #: Models accepted upstream. Empty means "accept anything" (local servers).
    #: Measured, not guessed — model names die: `gpt-5` stopped working between
    #: the third-party prior art being written and M0 (plan §8.1).
    valid_models: frozenset[str] = frozenset()

    #: Caller-facing aliases resolved before validation.
    aliases: dict[str, ModelAlias] = field(default_factory=dict)

    #: Fields forced into the upstream body, overriding whatever the caller sent.
    mandatory_body: dict[str, Any] = field(default_factory=dict)

    #: Parameters silently dropped. `temperature` is here for Codex because
    #: `_get_provider_kwargs` forwards it whenever config sets it and
    #: `.env.example` invites setting it, but reasoning models reject it.
    #: `max_tokens` is here because M0 measured
    #: `400 Unsupported parameter: max_output_tokens`.
    strip_params: frozenset[str] = frozenset()

    #: Static headers. Dynamic ones (session_id) are the adapter's job.
    headers: dict[str, str] = field(default_factory=dict)

    #: How an inbound `response_format` is expressed upstream. Note this is NOT
    #: a lever over which structured-output method langchain uses — that is
    #: decided client-side in `with_structured_output` before the helper sees
    #: anything (Codex review finding, round 1). It only governs translation of
    #: a `response_format` that does arrive.
    json_schema_style: str = "none"  # "none" | "responses_text_format" | "chat_response_format"

    #: Default reasoning effort when neither the alias nor the caller sets one.
    default_reasoning_effort: Optional[str] = None

    def resolve_model(self, requested: str) -> tuple[str, Optional[str]]:
        """Return ``(real_model, reasoning_effort)`` for a caller-supplied name.

        Raises ``ValueError`` listing valid names on rejection — a bare 400 from
        upstream tells the user nothing actionable, and model names change often
        enough that the error text is the documentation.
        """
        alias = self.aliases.get(requested)
        if alias is not None:
            return alias.real_model, alias.reasoning_effort or self.default_reasoning_effort
        if self.valid_models and requested not in self.valid_models:
            known = sorted(set(self.valid_models) | set(self.aliases))
            raise ValueError(
                f"model {requested!r} is not available on this provider. "
                f"Valid names: {', '.join(known)}"
            )
        return requested, self.default_reasoning_effort
