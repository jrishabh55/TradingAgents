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
# "interrupted" is a RESUMABLE state, distinct from "failed": the run did not
# error, the process went away underneath it (deploy, crash, laptop sleep). Only
# runs in this state are candidates for checkpoint resume.
RunStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]


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
    # Owner of this run. Populated by the auth middleware via store.create_run.
    # 'anonymous' for pre-auth (legacy) rows when no auth backend is configured.
    user_id: Optional[str] = None


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
    #: True for providers backed by a helper process the user must be running;
    #: the UI checks GET /api/helper/status before treating them as usable.
    requires_helper: bool = False
    #: True for BYOC providers (Gemini): the user must have their own
    #: credential — the UI checks GET /api/keys/gemini before allowing a run.
    requires_user_key: bool = False


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


# ---------------------------------------------------------------------------
# Risk levels (GET /api/runs/{id}/levels)
# ---------------------------------------------------------------------------


class LevelValue(BaseModel):
    """A price level plus the rule that produced it.

    ``basis`` exists so no number in this response is unexplained — the UI can
    show "how was this derived" next to every figure. These are risk-management
    arithmetic, not price predictions.
    """

    price: float
    basis: str


class ComputedLevels(BaseModel):
    """Deterministic levels derived from OHLCV — no LLM involved."""

    entry: LevelValue
    stop: LevelValue
    target: LevelValue
    # Secondary R-multiple target, shown alongside the primary one.
    target_alt: LevelValue
    risk_per_share: float
    risk_pct_of_entry: float
    reward_risk_ratio: float
    # Nearest resistance above entry, when one was found in the lookback.
    resistance: Optional[LevelValue] = None


class PositionSize(BaseModel):
    """Fixed-fractional sizing: the stop distance decides the size."""

    shares: int
    cash_risk: float
    position_value: float
    capital: float
    risk_pct: float
    basis: str


class ModelSuggested(BaseModel):
    """What the Trader agent asserted, for comparison. Not used in the math."""

    stop_loss: Optional[float] = None
    entry_price: Optional[float] = None
    position_sizing: Optional[str] = None


class LevelsResponse(BaseModel):
    """GET /api/runs/{id}/levels.

    Structure-based stop + risk-multiple target + fixed-fractional sizing,
    computed from the run's OHLCV as of its ``analysis_date``. Long side only
    in v1; a short-implying rating returns ``viable=false`` rather than
    long-shaped numbers.
    """

    run_id: str
    ticker: str
    analysis_date: str
    rating: Optional[str] = None
    # Quote currency of the instrument. ``capital`` is assumed to be in this
    # same currency — no FX conversion is performed. None when unresolvable.
    currency: Optional[str] = None
    viable: bool
    # Every reason the rules say don't take this trade (empty when viable).
    viability_notes: List[str] = Field(default_factory=list)
    levels: Optional[ComputedLevels] = None
    size: Optional[PositionSize] = None
    model_suggested: Optional[ModelSuggested] = None
    # Human-readable comparison of the agent's stop vs the computed one.
    divergence: Optional[str] = None
    disclaimer: str = (
        "Risk-management arithmetic derived from historical OHLCV, not a price "
        "prediction or investment advice. A level holding in the past does not "
        "mean it will hold again."
    )
