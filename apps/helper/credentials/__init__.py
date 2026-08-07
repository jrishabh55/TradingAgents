"""Credential sources — how the helper obtains a bearer for an upstream call.

Separate from ``UpstreamAdapter`` because the two vary independently: the same
Codex adapter is driven by a Codex-CLI file today and by our own OAuth tomorrow,
and the same OpenAI-compatible adapter is driven by an API key for OpenAI, a
different key for OpenRouter, and none at all for a local vLLM.

``get()`` is async because a real source may perform a network refresh. Tier A
never does (see :mod:`apps.helper.credentials.codex_file` for why), but the
protocol has to accommodate the ones that will.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Credential:
    """A bearer plus whatever account metadata the upstream needs in headers."""

    token: str
    #: Extra headers this credential implies, e.g. chatgpt-account-id.
    headers: dict[str, str] = field(default_factory=dict)
    #: For display only — never required to make a call.
    account_label: Optional[str] = None
    plan: Optional[str] = None
    #: Unix expiry of the bearer, when known.
    expires_at: Optional[int] = None

    def principal(self) -> str:
        """Stable, non-secret identity for cache-key binding.

        The idempotency cache must never serve one account's response to
        another, so its key includes this rather than the token itself.
        """
        return self.account_label or self.headers.get("chatgpt-account-id", "unknown")


class CredentialError(Exception):
    """Raised when no usable credential can be produced.

    ``remedy`` is shown to the user verbatim — the fix is almost always a
    specific command, so saying which one is the whole value of the error.
    """

    def __init__(self, message: str, *, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


@runtime_checkable
class CredentialSource(Protocol):
    """Obtains a usable credential, refreshing if that is safe for the source."""

    #: Short identifier used in logs and status output.
    name: str

    async def get(self) -> Credential:
        """Return a usable credential or raise :class:`CredentialError`."""
        ...
