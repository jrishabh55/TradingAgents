"""Build a ``TradingAgentsGraph`` from a ``RunRequest`` without touching upstream.

This is the only place in the webapp that imports the upstream graph package,
which means an upstream-side rename (e.g. of ``DEFAULT_CONFIG`` keys) only
breaks this one file.

Equivalent to the config-construction block in ``cli/main.py:973-985``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from cli.stats_handler import StatsCallbackHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from webapp.schemas import RunRequest


def build_graph_for_request(
    request: RunRequest,
) -> Tuple[TradingAgentsGraph, Dict[str, Any], Dict[str, Any], StatsCallbackHandler]:
    """Return ``(graph, init_state, stream_args, stats_handler)``.

    The stats handler is also bound to the graph, so the runner can periodically
    snapshot its counters and publish them as ``stats.update`` SSE events.
    Equivalent to ``cli/main.py:988-1000`` + ``:1091``.
    """
    config = _build_config(request)
    selected_analysts: List[str] = list(request.analysts)
    stats_handler = StatsCallbackHandler()

    graph = TradingAgentsGraph(
        selected_analysts,
        config=config,
        debug=False,
        callbacks=[stats_handler],
    )
    init_state = graph.propagator.create_initial_state(
        request.ticker, request.analysis_date
    )
    stream_args = graph.propagator.get_graph_args(callbacks=[stats_handler])
    return graph, init_state, stream_args, stats_handler


def _build_config(request: RunRequest) -> Dict[str, Any]:
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
    return config
