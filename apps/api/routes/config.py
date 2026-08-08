"""GET /api/config — drives the frontend's dropdowns.

Reads the same model catalog the CLI uses (``tradingagents.llm_clients.model_catalog``)
so the webapp never drifts from the CLI's options. The provider list mirrors
the hard-coded one in ``cli/utils.py:select_llm_provider`` (kept in lockstep
manually — that's a small constant table that rarely changes).
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Request

from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from apps.api.schemas import ConfigResponse, ModelOption, ProviderOption


router = APIRouter()


# (display, key, backend_url, capability flags) — mirrors cli/utils.py:233-244
_PROVIDERS = [
    ("OpenAI",       "openai",     "https://api.openai.com/v1",                                   {"reasoning_effort": True}),
    ("Google",       "google",     None,                                                           {"google_thinking": True}),
    ("Anthropic",    "anthropic",  "https://api.anthropic.com/",                                   {"anthropic_effort": True}),
    ("xAI",          "xai",        "https://api.x.ai/v1",                                          {}),
    ("DeepSeek",     "deepseek",   "https://api.deepseek.com",                                     {}),
    ("Qwen",         "qwen",       "https://dashscope.aliyuncs.com/compatible-mode/v1",            {}),
    ("GLM",          "glm",        "https://open.bigmodel.cn/api/paas/v4/",                        {}),
    ("OpenRouter",   "openrouter", "https://openrouter.ai/api/v1",                                 {}),
    ("Azure OpenAI", "azure",      None,                                                           {}),
    ("Ollama",       "ollama",     "http://localhost:11434/v1",                                    {}),
]


_OUTPUT_LANGUAGES = [
    {"label": "English (default)",    "value": "English"},
    {"label": "Chinese (中文)",        "value": "Chinese"},
    {"label": "Japanese (日本語)",     "value": "Japanese"},
    {"label": "Korean (한국어)",        "value": "Korean"},
    {"label": "Hindi (हिन्दी)",          "value": "Hindi"},
    {"label": "Spanish (Español)",     "value": "Spanish"},
    {"label": "Portuguese (Português)", "value": "Portuguese"},
    {"label": "French (Français)",      "value": "French"},
    {"label": "German (Deutsch)",      "value": "German"},
]


_ANALYSTS = [
    {"value": "market",        "label": "Market Analyst"},
    {"value": "social",        "label": "Social Media Analyst"},
    {"value": "news",          "label": "News Analyst"},
    {"value": "fundamentals",  "label": "Fundamentals Analyst"},
]


_RESEARCH_DEPTHS = [
    {"value": 1, "label": "Shallow (1 round, fastest)"},
    {"value": 2, "label": "Medium (2 rounds)"},
    {"value": 3, "label": "Deep (3 rounds)"},
    {"value": 4, "label": "Very Deep (4 rounds)"},
    {"value": 5, "label": "Exhaustive (5 rounds)"},
]


def _models_for_provider(provider_key: str) -> List[ModelOption]:
    """Union of quick+deep model lists for a provider, dedup'd, in catalog order."""
    catalog = MODEL_OPTIONS.get(provider_key, {})
    seen: set = set()
    out: List[ModelOption] = []
    for mode in ("quick", "deep"):
        for label, value in catalog.get(mode, ()):
            if value in seen:
                continue
            seen.add(value)
            out.append(ModelOption(id=value, label=label))
    return out


@router.get("/config", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    providers = [
        ProviderOption(
            key=key,
            label=label,
            backend_url=backend_url,
            supports_reasoning_effort=caps.get("reasoning_effort", False),
            supports_google_thinking=caps.get("google_thinking", False),
            supports_anthropic_effort=caps.get("anthropic_effort", False),
        )
        for label, key, backend_url, caps in _PROVIDERS
    ]
    models_by_provider = {key: _models_for_provider(key) for _, key, _, _ in _PROVIDERS}

    # The helper appears as its own provider so the user never has to paste a
    # URL. ALWAYS listed (first): whether the user's helper is local, on the
    # relay, or not installed yet is a runtime question the UI answers via
    # /api/helper/status — hiding the provider would hide the setup path too.
    # Its models come from the provider's quirks row, not a hardcoded list.
    from apps.api.integrations.helper_backend import (
        HELPER_PROVIDER_KEY,
        HELPER_PROVIDER_LABEL,
        helper_models,
    )

    providers.insert(
        0,
        ProviderOption(
            key=HELPER_PROVIDER_KEY,
            label=HELPER_PROVIDER_LABEL,
            backend_url=None,
            # Effort rides in the model name (see the alias table), so there
            # is no separate effort control for this provider.
            supports_reasoning_effort=False,
            supports_google_thinking=False,
            supports_anthropic_effort=False,
            requires_helper=True,
        ),
    )
    models_by_provider[HELPER_PROVIDER_KEY] = [
        ModelOption(id=value, label=label) for label, value in helper_models()
    ]
    return ConfigResponse(
        analysts=_ANALYSTS,
        research_depths=_RESEARCH_DEPTHS,
        providers=providers,
        models_by_provider=models_by_provider,
        output_languages=_OUTPUT_LANGUAGES,
        default_ticker="SPY",
    )


def _helper_dist_file(user_agent: str = "") -> "Path":
    """The packaged helper artifact this API can serve itself — best match
    for the requesting OS when several builds sit in ``dist/``.

    ``apps/helper/packaging/build.sh`` (macOS) and ``build.ps1`` (Windows)
    drop artifacts in ``dist/`` at the repo root; ``TA_HELPER_DIST_FILE``
    overrides everything with a single explicit file.
    """
    import os
    from pathlib import Path

    explicit = os.environ.get("TA_HELPER_DIST_FILE", "")
    if explicit:
        return Path(explicit)
    dist = Path(
        os.environ.get("TA_HELPER_DIST_DIR", "")
        or Path(__file__).resolve().parents[3] / "dist"
    )
    if "windows" in user_agent.lower():
        names = ["DrishtiHelperSetup.exe", "DrishtiHelper-windows.zip"]
    else:
        names = ["DrishtiHelper.dmg"]
    names.append("DrishtiHelper.zip")  # generic fallback
    for name in names:
        f = dist / name
        if f.is_file():
            return f
    return dist / "DrishtiHelper.zip"


@router.get("/helper/download")
def helper_download(request: Request):
    """Serve the packaged helper app. Unauthenticated by design (see
    auth._BYPASS_PATHS): it's a public artifact a not-yet-set-up user needs,
    and a bare <a href> can't carry a bearer token anyway."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    f = _helper_dist_file(request.headers.get("user-agent", ""))
    if not f.is_file():
        raise HTTPException(
            status_code=404,
            detail="no helper build available — run apps/helper/packaging/build.sh",
        )
    media = {
        ".dmg": "application/x-apple-diskimage",
        ".exe": "application/octet-stream",
    }.get(f.suffix, "application/zip")
    return FileResponse(f, media_type=media, filename=f.name)


@router.get("/helper/version")
def helper_version() -> dict:
    """Latest helper version + where to get it. Unauthenticated (see
    auth._BYPASS_PATHS): running helpers poll this for update checks and
    carry a relay pairing token, not a Clerk JWT. The deployed API's own
    tree defines "latest" — the helper package lives in this same repo."""
    import os

    from apps.helper.version import __version__

    download_url = os.environ.get("TA_HELPER_DOWNLOAD_URL", "")
    if not download_url and _helper_dist_file().is_file():
        download_url = "/api/helper/download"
    return {"version": __version__, "download_url": download_url}


@router.get("/helper/status")
def helper_status(request: Request) -> dict:
    """Whether the current user's helper is actually reachable right now.

    ``helper_enabled()`` only proves a token file exists; the UI needs to know
    whether selecting the helper provider would work *before* a run fails deep
    in the pipeline with a connection error. Sync def on purpose: FastAPI runs
    it in the threadpool, so the blocking loopback probe never stalls the loop.
    """
    from apps.api.auth import current_user_id
    from apps.api.integrations.helper_backend import (
        helper_enabled,
        local_helper_reachable,
    )
    from apps.api.relay import get_relay_registry

    import os

    # Where the UI sends users who don't have the helper yet. Explicit env
    # wins (artifact hosted elsewhere, e.g. a release page); otherwise a build
    # sitting in dist/ is served by this API's own download route, so the link
    # appears the moment `apps/helper/packaging/build.sh` has run.
    download_url = os.environ.get("TA_HELPER_DOWNLOAD_URL", "")
    if not download_url and _helper_dist_file().is_file():
        download_url = "/api/helper/download"

    # Same precedence as graph_factory's routing, so what this reports is what
    # a run would actually use: live local helper first, then the relay.
    if local_helper_reachable():
        return {"enabled": True, "mode": "local", "connected": True,
                "download_url": download_url}
    user_id = current_user_id(request)
    if get_relay_registry().is_connected(user_id):
        return {"enabled": True, "mode": "relay", "connected": True,
                "download_url": download_url}
    if helper_enabled():
        # Configured (token file / env) but not answering — stopped daemon.
        return {"enabled": True, "mode": "local", "connected": False,
                "download_url": download_url}
    return {"enabled": False, "mode": None, "connected": False,
            "download_url": download_url}
