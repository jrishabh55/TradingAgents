"""Provider registry.

`Provider = (name, adapter, credentials, quirks)`. Adding a provider is one
module plus one entry here — the ``EchoAdapter`` seam test fails if that stops
being true.

Quirks are a field, not adapter attributes, because one adapter serves several
providers that differ only by deployment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

from apps.helper.credentials import Credential, CredentialSource
from apps.helper.quirks import ProviderQuirks
from apps.helper.types import NormalizedRequest, NormalizedResponse


@dataclass
class CallContext:
    """Per-call deadline and cancellation.

    Without this an adapter cannot abort an in-flight upstream request, and the
    pipeline's cancellation is only polled between graph nodes — so a cancelled
    run would keep streaming and keep billing.
    """

    timeout_s: float = 600.0
    #: Returns True when the caller has gone away and the call should abort.
    is_cancelled: Callable[[], bool] = lambda: False


@runtime_checkable
class UpstreamAdapter(Protocol):
    """Translates a NormalizedRequest to an upstream call and back."""

    name: str

    async def send(
        self,
        req: NormalizedRequest,
        cred: Credential,
        quirks: ProviderQuirks,
        ctx: CallContext,
    ) -> NormalizedResponse:
        ...


@dataclass(frozen=True)
class Provider:
    name: str
    adapter: UpstreamAdapter
    quirks: ProviderQuirks
    #: Tried in order; the first ``available()`` source wins. Sources without an
    #: ``available()`` method are always considered available.
    credentials: tuple[CredentialSource, ...]

    async def credential(self) -> Credential:
        from apps.helper.credentials import CredentialError

        errors: list[str] = []
        for src in self.credentials:
            check = getattr(src, "available", None)
            if callable(check) and not check():
                errors.append(f"{src.name}: not configured")
                continue
            try:
                return await src.get()
            except CredentialError as exc:
                errors.append(f"{src.name}: {exc.message}")
        remedy = ""
        for src in self.credentials:
            try:
                await src.get()
            except CredentialError as exc:
                if exc.remedy:
                    remedy = exc.remedy
                    break
            except Exception:  # noqa: BLE001
                continue
        raise CredentialError(
            f"no usable credential for provider {self.name!r} ({'; '.join(errors)})",
            remedy=remedy,
        )


class Registry:
    """Name -> Provider. Deliberately a plain dict, not a plugin loader."""

    def __init__(self, providers: Iterable[Provider] = ()) -> None:
        self._providers: dict[str, Provider] = {p.name: p for p in providers}

    def register(self, provider: Provider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[Provider]:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)


def default_registry(**overrides: Any) -> Registry:
    """The providers shipped by default.

    Imported lazily so a test can build a registry without pulling httpx or
    touching the filesystem.
    """
    from apps.helper.credentials.api_key import ApiKeySource
    from apps.helper.credentials.codex_file import CodexAuthFileSource
    from apps.helper.providers.codex import CODEX_QUIRKS, CodexResponsesAdapter

    codex = Provider(
        name="codex",
        adapter=overrides.get("codex_adapter") or CodexResponsesAdapter(),
        quirks=CODEX_QUIRKS,
        # Tier A first: reusing an existing `codex login` is zero-friction.
        # Tier B (own OAuth) slots in here once U1 is resolved.
        credentials=(CodexAuthFileSource(),),
    )
    return Registry([codex])


__all__ = [
    "CallContext",
    "Provider",
    "Registry",
    "UpstreamAdapter",
    "default_registry",
]
