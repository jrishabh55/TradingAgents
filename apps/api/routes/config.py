"""GET /api/config — drives the frontend's dropdowns.

Reads the same model catalog the CLI uses (``tradingagents.llm_clients.model_catalog``)
so the webapp never drifts from the CLI's options. The provider list mirrors
the hard-coded one in ``cli/utils.py:select_llm_provider`` (kept in lockstep
manually — that's a small constant table that rarely changes).
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter

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

    # The local helper appears as its own provider so the user never has to paste
    # a URL. Its models come from the provider's quirks row, not a hardcoded
    # list — model choice stays with the user, per run. Listed first because when
    # it IS running it is usually the intended option.
    from apps.api.integrations.helper_backend import (
        HELPER_PROVIDER_KEY,
        HELPER_PROVIDER_LABEL,
        helper_base_url,
        helper_enabled,
        helper_models,
    )

    if helper_enabled():
        providers.insert(
            0,
            ProviderOption(
                key=HELPER_PROVIDER_KEY,
                label=HELPER_PROVIDER_LABEL,
                backend_url=helper_base_url(),
                # Effort rides in the model name (see the alias table), so there
                # is no separate effort control for this provider.
                supports_reasoning_effort=False,
                supports_google_thinking=False,
                supports_anthropic_effort=False,
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
