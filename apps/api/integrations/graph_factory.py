"""Build a ``TradingAgentsGraph`` from a ``RunRequest`` without touching upstream.

This is the only place in apps/api/ that imports the upstream graph package,
which means an upstream-side rename (e.g. of ``DEFAULT_CONFIG`` keys) only
breaks this one file.

Equivalent to the config-construction block in ``cli/main.py:973-985``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cli.models import AnalystType
from cli.stats_handler import StatsCallbackHandler
from cli.utils import detect_asset_type, filter_analysts_for_asset_type
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from apps.api.schemas import RunRequest


# Filesystem-safe characters for user_id directory components. Clerk user ids
# look like ``user_2abcXYZ`` — already safe — but defensive sanitization
# handles synthetic ids like 'shared-bearer' and any future scheme cleanly.
_USER_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_user_dir(user_id: str) -> str:
    """Sanitize a user id into a single filesystem-safe directory component."""
    sanitized = _USER_ID_SAFE_RE.sub("_", user_id).strip("_")
    return sanitized or "anonymous"


def effective_analysts(request: RunRequest) -> List[str]:
    """The analysts that will actually run, after asset-type filtering.

    The CLI drops the Fundamentals analyst on crypto tickers (a coin has no
    balance sheet), so the API has to do the same or agents get asked for
    fundamentals that don't exist. Both the graph and the SSE translator read
    this, so the pipeline and the UI can never disagree about which panels to
    expect. Pure string work — cheap enough to call more than once.
    """
    asset_type = detect_asset_type(request.ticker)
    kept = filter_analysts_for_asset_type(
        [AnalystType(a) for a in request.analysts], asset_type
    )
    return [a.value for a in kept]


def build_graph_for_request(
    request: RunRequest,
    *,
    user_id: Optional[str] = None,
) -> Tuple[TradingAgentsGraph, Dict[str, Any], Dict[str, Any], StatsCallbackHandler]:
    """Return ``(graph, init_state, stream_args, stats_handler)``.

    The stats handler is also bound to the graph, so the runner can periodically
    snapshot its counters and publish them as ``stats.update`` SSE events.
    Equivalent to ``cli/main.py:988-1000`` + ``:1091``.

    When ``user_id`` is provided, the graph is configured with a per-user
    memory log path so concurrent runs from different users do not race the
    single-tenant on-disk file. Same-user concurrent runs still need
    serialization (handled by the runner's per-user lock).
    """
    config = _build_config(request, user_id=user_id)
    selected_analysts: List[str] = effective_analysts(request)
    stats_handler = StatsCallbackHandler()

    # The local helper is selected as its own provider key in the UI, then mapped
    # here onto openai_compatible + the helper's base URL. The credential is
    # resolved at construction time and never enters `config`, because
    # store.create_run persists request.model_dump() as config_json — a token on
    # RunRequest would be written to SQLite in plaintext and also reach SSE
    # events and saved reports.
    from apps.api.integrations.helper_backend import (
        HelperBackedGraph,
        helper_base_url,
        helper_credential,
        is_helper_provider,
    )

    graph_cls: Any = TradingAgentsGraph
    extra: Dict[str, Any] = {}
    if is_helper_provider(request.llm_provider):
        config["llm_provider"] = "openai_compatible"
        config["backend_url"] = request.backend_url or helper_base_url()
        graph_cls = HelperBackedGraph
        extra["api_key"] = helper_credential()

    graph = graph_cls(
        selected_analysts,
        config=config,
        debug=False,
        callbacks=[stats_handler],
        **extra,
    )
    # Mirror TradingAgentsGraph._run_graph's state wiring, since the runner
    # streams graph.graph.stream() directly instead of calling propagate():
    # detect the asset type from the ticker (as the CLI does) and resolve the
    # instrument identity once, so agents anchor to the real company rather
    # than pattern-matching the price chart to a wrong one.
    #
    # Not mirrored: _run_graph also passes past_context (the memory log's prior
    # runs for this ticker), so API runs are not influenced by earlier runs the
    # way CLI runs are. Open product question — the per-user log is currently
    # write-only from the API's perspective.
    asset_type = detect_asset_type(request.ticker).value
    init_state = graph.propagator.create_initial_state(
        request.ticker,
        request.analysis_date,
        asset_type=asset_type,
        instrument_context=graph.resolve_instrument_context(request.ticker, asset_type),
    )
    stream_args = graph.propagator.get_graph_args(callbacks=[stats_handler])
    return graph, init_state, stream_args, stats_handler


def _build_config(request: RunRequest, *, user_id: Optional[str] = None) -> Dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = request.research_depth
    config["max_risk_discuss_rounds"] = request.research_depth
    config["quick_think_llm"] = request.shallow_thinker
    config["deep_think_llm"] = request.deep_thinker
    config["backend_url"] = request.backend_url
    config["llm_provider"] = request.llm_provider.lower()
    config["google_thinking_level"] = request.google_thinking_level
    config["openai_reasoning_effort"] = request.openai_reasoning_effort
    config["anthropic_effort"] = request.anthropic_effort
    config["output_language"] = request.output_language
    config["checkpoint_enabled"] = request.checkpoint_enabled
    if user_id:
        # Per-user memory log so different users' runs never share the file.
        # The upstream TradingMemoryLog reads `memory_log_path` from config —
        # we set it without touching any upstream code.
        base_dir = os.environ.get(
            "WEBAPP_PER_USER_MEMORY_DIR",
            str(Path.home() / ".tradingagents" / "memory_per_user"),
        )
        config["memory_log_path"] = str(
            Path(base_dir) / _safe_user_dir(user_id) / "trading_memory.md"
        )
    return config
