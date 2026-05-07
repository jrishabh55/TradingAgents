"""Render a TradingAgents run's final state to a Markdown report.

Used by both the HTTP download endpoint (``GET /api/runs/{id}/report.md``) and
the runner's on-disk export. Same shape as the CLI's ``complete_report.md``
written by ``cli/main.py:save_report_to_disk`` so users moving between CLI
and webapp see consistent files.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


def render_markdown_report(
    *,
    ticker: str,
    analysis_date: str,
    rating: Optional[str],
    final_state: Dict[str, Any],
    generated_at: Optional[str] = None,
) -> str:
    """Build the Markdown body. ``final_state`` may be partial (mid-run)."""
    parts: List[str] = []
    parts.append(f"# Trading Analysis Report: {ticker}")
    parts.append("")
    parts.append(f"Generated: {generated_at or datetime.now().isoformat(timespec='seconds')}")
    parts.append(f"Analysis date: {analysis_date}")
    if rating:
        parts.append("")
        parts.append(f"**Final Rating: {rating}**")
    parts.append("")

    market = final_state.get("market_report")
    sentiment = final_state.get("sentiment_report")
    news = final_state.get("news_report")
    fundamentals = final_state.get("fundamentals_report")
    if any([market, sentiment, news, fundamentals]):
        parts.append("## I. Analyst Team Reports")
        parts.append("")
        if market:
            parts.append("### Market Analyst"); parts.append(market); parts.append("")
        if sentiment:
            parts.append("### Social Analyst"); parts.append(sentiment); parts.append("")
        if news:
            parts.append("### News Analyst"); parts.append(news); parts.append("")
        if fundamentals:
            parts.append("### Fundamentals Analyst"); parts.append(fundamentals); parts.append("")

    debate = final_state.get("investment_debate_state") or {}
    if debate.get("bull_history") or debate.get("bear_history") or debate.get("judge_decision"):
        parts.append("## II. Research Team")
        parts.append("")
        if debate.get("bull_history"):
            parts.append("### Bull Researcher"); parts.append(debate["bull_history"]); parts.append("")
        if debate.get("bear_history"):
            parts.append("### Bear Researcher"); parts.append(debate["bear_history"]); parts.append("")
        if debate.get("judge_decision"):
            parts.append("### Research Manager"); parts.append(debate["judge_decision"]); parts.append("")

    trader_plan = final_state.get("trader_investment_plan")
    if trader_plan:
        parts.append("## III. Trading Team")
        parts.append("")
        parts.append("### Trader"); parts.append(trader_plan); parts.append("")

    risk = final_state.get("risk_debate_state") or {}
    if risk.get("aggressive_history") or risk.get("conservative_history") or risk.get("neutral_history"):
        parts.append("## IV. Risk Management Team")
        parts.append("")
        if risk.get("aggressive_history"):
            parts.append("### Aggressive Analyst"); parts.append(risk["aggressive_history"]); parts.append("")
        if risk.get("conservative_history"):
            parts.append("### Conservative Analyst"); parts.append(risk["conservative_history"]); parts.append("")
        if risk.get("neutral_history"):
            parts.append("### Neutral Analyst"); parts.append(risk["neutral_history"]); parts.append("")

    final_decision = final_state.get("final_trade_decision")
    if final_decision:
        parts.append("## V. Portfolio Manager Decision")
        parts.append("")
        parts.append(final_decision)
        parts.append("")

    return "\n".join(parts)
