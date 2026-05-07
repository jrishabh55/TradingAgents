"""Pydantic schemas for the apps/api REST + SSE API.

The fields on ``RunRequest`` are a 1:1 mirror of the ``selections`` dict the CLI
builds in ``cli/main.py:599-612`` — the only translation step is dropping the
"selected_" prefix and passing the values into ``DEFAULT_CONFIG`` exactly as the
CLI does at ``cli/main.py:973-985``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# Status vocabulary for jobs. Aligned with the SSE event taxonomy in
# apps/api/jobs/translator.py.
RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


# Fields that participate in the cache key. Anything that changes the
# pipeline's *output* must be in here. ``checkpoint_enabled`` is intentionally
# excluded — it's an internal LangGraph mechanism (resume on crash) and does
# not affect report content.
_CACHE_KEY_FIELDS = (
    "ticker",
    "analysis_date",
    "analysts",
    "research_depth",
    "llm_provider",
    "backend_url",
    "shallow_thinker",
    "deep_thinker",
    "google_thinking_level",
    "openai_reasoning_effort",
    "anthropic_effort",
    "output_language",
)


def canonicalize_request(request: "RunRequest") -> Dict[str, Any]:
    """Return a deterministic dict suitable for hashing.

    - Subsets to fields that affect output (drops ``checkpoint_enabled``)
    - Sorts ``analysts`` so [news, market] and [market, news] hash identically
    - Lowercases ``llm_provider`` so "OpenAI" and "openai" collide

    Two requests with the same canonical form will produce the same cached
    report (subject to the TTL — markets and news data shift over time).
    """
    raw = request.model_dump()
    canonical: Dict[str, Any] = {k: raw[k] for k in _CACHE_KEY_FIELDS}
    canonical["analysts"] = sorted(canonical["analysts"])
    if isinstance(canonical.get("llm_provider"), str):
        canonical["llm_provider"] = canonical["llm_provider"].lower()
    return canonical


def request_hash(request: "RunRequest") -> str:
    """SHA-256 hex of the canonical request — the cache key."""
    payload = json.dumps(canonicalize_request(request), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunRequest(BaseModel):
    """Body of POST /api/runs — every field maps to a config key the graph reads."""

    ticker: str = Field(..., description="Exchange-qualified ticker (e.g. RELIANCE.NS, AAPL).")
    analysis_date: str = Field(..., description="YYYY-MM-DD; must not be in the future.")
    analysts: List[str] = Field(
        ...,
        description="Subset of {market, social, news, fundamentals}. Order is normalized server-side.",
        min_length=1,
    )
    research_depth: int = Field(1, ge=1, le=5, description="Both max_debate_rounds and max_risk_discuss_rounds.")
    llm_provider: str = Field(..., description="openai | anthropic | google | deepseek | ...")
    backend_url: Optional[str] = Field(None, description="Provider base_url override; None means provider default.")
    shallow_thinker: str = Field(..., description="Model id for the quick-thinking LLM.")
    deep_thinker: str = Field(..., description="Model id for the deep-thinking LLM.")
    google_thinking_level: Optional[str] = Field(None, description="low | medium | high — only for Google.")
    openai_reasoning_effort: Optional[str] = Field(None, description="low | medium | high — only for OpenAI reasoning models.")
    anthropic_effort: Optional[str] = Field(None, description="high | minimal — only for Anthropic thinking-mode.")
    output_language: str = Field("English", description="Free-text language label injected into agent prompts.")
    checkpoint_enabled: bool = Field(False, description="If true, LangGraph SqliteSaver enables resume on crash.")


class RunSummary(BaseModel):
    """Row in GET /api/runs."""

    id: str
    ticker: str
    analysis_date: str
    status: RunStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    rating: Optional[str] = None
    error: Optional[str] = None
    # True when this row is being returned as a cache hit for an incoming
    # POST /api/runs — i.e. the pipeline did not run again. The frontend
    # uses this to surface a "Cached result" banner with a force-refresh
    # action.
    cached: bool = False


class RunDetail(BaseModel):
    """Full payload of GET /api/runs/{id}."""

    id: str
    ticker: str
    analysis_date: str
    status: RunStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    rating: Optional[str] = None
    decision_text: Optional[str] = None
    error: Optional[str] = None
    config: Dict[str, Any]
    # Subset of AgentState that's a string report — small enough to ship inline.
    market_report: Optional[str] = None
    sentiment_report: Optional[str] = None
    news_report: Optional[str] = None
    fundamentals_report: Optional[str] = None
    investment_plan: Optional[str] = None
    trader_investment_plan: Optional[str] = None
    final_trade_decision: Optional[str] = None
    investment_debate_state: Optional[Dict[str, Any]] = None
    risk_debate_state: Optional[Dict[str, Any]] = None
    # True when this run is being returned as a cache hit. See RunSummary.cached.
    cached: bool = False


class EventEnvelope(BaseModel):
    """One row of the SSE stream. Persisted in the events table for replay."""

    seq: int
    ts: datetime
    type: str
    data: Dict[str, Any] = Field(default_factory=dict)


class ProviderOption(BaseModel):
    key: str
    label: str
    backend_url: Optional[str] = None
    supports_reasoning_effort: bool = False
    supports_google_thinking: bool = False
    supports_anthropic_effort: bool = False


class ModelOption(BaseModel):
    id: str
    label: str


class ConfigResponse(BaseModel):
    """GET /api/config — drives the frontend dropdowns."""

    analysts: List[Dict[str, str]]
    research_depths: List[Dict[str, Any]]
    providers: List[ProviderOption]
    # provider key -> list of models
    models_by_provider: Dict[str, List[ModelOption]]
    output_languages: List[Dict[str, str]]
    default_ticker: str = "SPY"
