"""Point the pipeline at drishti-helper without touching upstream.

Two problems to solve, both from the Codex review:

1. ``_get_provider_kwargs`` does not forward ``api_key``, even though
   ``api_key`` IS in the OpenAI client's ``_PASSTHROUGH_KWARGS``. Fixed by
   subclassing ``TradingAgentsGraph`` and extending that one method — the
   wrap-don't-edit rule from CLAUDE.md.

2. The credential must never travel on ``RunRequest``. ``store.create_run``
   persists ``request.model_dump()`` as ``config_json``, so putting it there
   would write the helper token into SQLite in plaintext, and it would also
   reach SSE events and saved reports. It is resolved here, at graph
   construction, and lives only in memory.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from tradingagents.graph.trading_graph import TradingAgentsGraph

#: Base URL of a running helper, including the provider path segment.
HELPER_URL_ENV = "TA_HELPER_URL"
#: Explicit token override; otherwise the token file is read.
HELPER_TOKEN_ENV = "TA_HELPER_TOKEN"

DEFAULT_HELPER_URL = "http://127.0.0.1:8899/v1/codex"


class HelperBackedGraph(TradingAgentsGraph):
    """``TradingAgentsGraph`` that forwards in-memory provider kwargs.

    Credentials (helper token as ``api_key``, a user's Gemini key as
    ``api_key``, a Google OAuth ``credentials`` object) are held on the
    instance and injected into the provider kwargs, so they reach the LLM
    client without passing through config that gets serialized.
    """

    def __init__(
        self,
        *args: Any,
        api_key: Optional[str] = None,
        provider_kwargs: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        # Set before super().__init__, because the base constructor builds the
        # LLM clients and therefore calls _get_provider_kwargs immediately.
        self._extra_provider_kwargs = dict(provider_kwargs or {})
        if api_key:
            self._extra_provider_kwargs["api_key"] = api_key
        super().__init__(*args, **kwargs)

    def _get_provider_kwargs(self) -> dict[str, Any]:
        kwargs = super()._get_provider_kwargs()
        for key, value in self._extra_provider_kwargs.items():
            if value:
                kwargs[key] = value
        return kwargs


def helper_credential() -> Optional[str]:
    """The helper's local bearer token, or None when no helper is configured.

    Reads the shared token file so a local setup needs zero configuration —
    the helper generates it on first run and the API picks it up.
    """
    explicit = os.environ.get(HELPER_TOKEN_ENV)
    if explicit:
        return explicit
    from apps.helper import paths

    return paths.read_secret(paths.local_token_file())


def helper_base_url() -> str:
    return os.environ.get(HELPER_URL_ENV, DEFAULT_HELPER_URL)


def helper_enabled() -> bool:
    """Whether runs should be routed through the LOCAL (loopback) helper.

    Opt-in via ``TA_HELPER_URL``, or implicitly when a token file exists — a
    local user who has started the helper should not also have to set env vars.
    A user whose helper connects over the relay is covered by
    :func:`relay_available` instead.
    """
    if os.environ.get(HELPER_URL_ENV):
        return True
    return bool(helper_credential())


#: Where the pipeline's worker thread reaches this API's own relay shim. The
#: shim is plain HTTP on this same server; the worker just needs a routable
#: address for it (uvicorn's port isn't knowable from here).
SELF_URL_ENV = "WEBAPP_SELF_URL"
DEFAULT_SELF_URL = "http://127.0.0.1:8080"


def relay_shim_url() -> str:
    base = os.environ.get(SELF_URL_ENV, DEFAULT_SELF_URL).rstrip("/")
    return f"{base}/internal/relay/v1/codex"


def relay_available(user_id: Optional[str]) -> bool:
    """Whether ``user_id``'s own helper is connected over the relay right now."""
    from apps.api.relay import get_relay_registry

    return get_relay_registry().is_connected(user_id or "anonymous")


def local_helper_reachable(timeout_s: float = 1.0) -> bool:
    """Whether the configured local helper is actually serving right now.

    ``helper_enabled()`` only proves configuration (a token file can outlive
    the daemon); routing a run at a dead endpoint fails on its first LLM call.
    Probes the unauthenticated /healthz liveness route.
    """
    if not helper_enabled():
        return False
    import urllib.error
    import urllib.parse
    import urllib.request

    parts = urllib.parse.urlsplit(helper_base_url())
    probe = f"{parts.scheme}://{parts.netloc}/healthz"
    try:
        with urllib.request.urlopen(probe, timeout=timeout_s) as res:
            return 200 <= res.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def local_helper_version(timeout_s: float = 1.0) -> Optional[str]:
    """Version the LOCAL helper reports on /healthz, or None.

    None covers both "not reachable" and "predates version reporting" —
    callers treat unknown as outdated, which is exactly right for old
    builds.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    parts = urllib.parse.urlsplit(helper_base_url())
    probe = f"{parts.scheme}://{parts.netloc}/healthz"
    try:
        with urllib.request.urlopen(probe, timeout=timeout_s) as res:
            payload = json.loads(res.read())
        version = payload.get("version") if isinstance(payload, dict) else None
        return str(version) if version else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def helper_ready(user_id: Optional[str]) -> bool:
    """A live local helper OR this user's helper connected over the relay."""
    return local_helper_reachable() or relay_available(user_id)


#: Provider key the UI sends when the user picks the local helper. Mapped to
#: ``openai_compatible`` plus the helper's base URL at graph-construction time,
#: so the existing per-run plumbing carries it and no RunRequest field is added
#: (a new field would be persisted into config_json — see the module docstring).
HELPER_PROVIDER_KEY = "chatgpt_helper"

#: Display label for the UI dropdown.
HELPER_PROVIDER_LABEL = "ChatGPT subscription (local helper)"


def is_helper_provider(provider: str) -> bool:
    return (provider or "").strip().lower() == HELPER_PROVIDER_KEY


def helper_models() -> list[tuple[str, str]]:
    """``(label, value)`` pairs the helper accepts, for the UI dropdown.

    Derived from the provider's quirks row rather than hardcoded, so the user
    keeps choosing the model per run. Real model names come first; the
    effort-suffixed aliases follow, then the two convenience presets.
    """
    from apps.helper.providers.codex import CODEX_QUIRKS

    real = sorted(CODEX_QUIRKS.valid_models)
    presets = [a for a in CODEX_QUIRKS.aliases if "-" not in a.removeprefix("ta-")]
    suffixed = sorted(a for a in CODEX_QUIRKS.aliases if a not in presets)
    out: list[tuple[str, str]] = [(m, m) for m in real]
    out += [(f"{a}  (preset)", a) for a in sorted(presets)]
    out += [(a, a) for a in suffixed]
    return out
