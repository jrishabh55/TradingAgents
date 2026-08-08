"""Translate ``graph.stream()`` chunks into structured SSE events.

This is the webapp equivalent of the chunk-handling block in
``cli/main.py:1095-1187``: same logic for figuring out which agent just
produced output, what stage we're in, and what content to surface — but emits
``EventEnvelope`` objects instead of mutating a Rich layout.

The translator is **stateful per run** because the upstream graph emits
*incremental* updates (e.g. risk-debate state grows over chunks; we only want
to publish the new fragment), and because some transitions ("research_team
completed → trader started") are derived from "this chunk first contained
field X, prior chunks didn't."

We don't share state across runs: callers create one ``ChunkTranslator`` per
run and feed it chunks in order.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Set

from apps.api.schemas import EventEnvelope


logger = logging.getLogger(__name__)


# Mirrors cli/main.py:837-849 — kept here so the webapp doesn't import from cli/.
ANALYST_ORDER: List[str] = ["market", "social", "news", "fundamentals"]
ANALYST_REPORT_MAP: Dict[str, str] = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


class ChunkTranslator:
    """Convert a sequence of LangGraph chunks into a stream of EventEnvelopes.

    The runner loops:

        for chunk in graph.graph.stream(init_state, **args):
            for env in translator.handle_chunk(chunk):
                store.append_event(...); bus.publish(...)

    Final state is accumulated in ``self.final_state`` so the runner can persist
    it after the loop ends without re-walking the trace.
    """

    def __init__(
        self,
        run_id: str,
        *,
        selected_analysts: List[str],
        start_seq: int = 0,
        replay_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.run_id = run_id
        self.selected_analysts = [a for a in ANALYST_ORDER if a in selected_analysts]
        # On resume this is seeded from the store. A fresh translator restarting
        # at 0 would collide with the events already persisted for this run,
        # violating the UNIQUE(run_id, seq) constraint and corrupting replay
        # ordering for any client reconnecting to the stream.
        self._seq = start_seq
        self._processed_message_ids: Set[str] = set()
        # Track which agents we've already announced as completed/started so we
        # don't emit duplicate transition events on every chunk.
        self._analyst_completed: Set[str] = set()
        self._announced_started: Set[str] = set()
        self._research_team_started = False
        self._research_team_completed = False
        self._trader_started = False
        self._trader_completed = False
        self._risk_started: Set[str] = set()
        self._risk_completed: Set[str] = set()
        self._portfolio_completed = False
        # Accumulating fragment lengths so we can publish only the new chars.
        self._debate_lengths: Dict[str, int] = {}
        # Accumulated final state — last-write-wins for each top-level key.
        # Seeded on resume from the snapshot the interrupted attempt persisted,
        # so sections it already produced are treated as known rather than
        # re-emitted as if new.
        self.final_state: Dict[str, Any] = dict(replay_state or {})
        # Mark analysts whose report already exists as completed, so a resume
        # does not re-announce started/completed for work that is done.
        for analyst_key, report_key in ANALYST_REPORT_MAP.items():
            if self.final_state.get(report_key):
                self._analyst_completed.add(analyst_key)
                self._announced_started.add(analyst_key)
        # Same for the debate machinery: without seeding the accumulated
        # fragment lengths and team flags, the first post-resume chunk (values
        # mode carries the FULL state) would re-emit every debate turn and
        # team transition the interrupted attempt already streamed.
        inv = self.final_state.get("investment_debate_state") or {}
        risk = self.final_state.get("risk_debate_state") or {}
        for key, text in (
            ("investment.bull", inv.get("bull_history")),
            ("investment.bear", inv.get("bear_history")),
            ("risk.aggressive", risk.get("aggressive_history")),
            ("risk.conservative", risk.get("conservative_history")),
            ("risk.neutral", risk.get("neutral_history")),
        ):
            if text:
                self._debate_lengths[key] = len(str(text).strip())
        if inv.get("bull_history") or inv.get("bear_history"):
            self._research_team_started = True
        if inv.get("judge_decision"):
            self._research_team_completed = True
        if self.final_state.get("trader_investment_plan"):
            self._trader_started = True
            self._trader_completed = True
        if any(
            risk.get(k)
            for k in ("aggressive_history", "conservative_history", "neutral_history")
        ):
            self._risk_started.add("risk")
        if risk.get("judge_decision"):
            self._portfolio_completed = True
        # Not seeded: _processed_message_ids — event payloads don't persist
        # message ids, so raw message/tool.called events from before the
        # interruption may re-emit once. Cosmetic (ticker panel only); every
        # report/debate/transition event above is properly deduplicated.

    # ---------- public ----------

    def emit_run_started(self, *, ticker: str, analysis_date: str, config: Dict[str, Any]) -> EventEnvelope:
        return self._event(
            "run.started",
            {"ticker": ticker, "analysis_date": analysis_date, "selected_analysts": self.selected_analysts, "config": config},
        )

    def emit_run_failed(self, error: str, traceback: Optional[str] = None) -> EventEnvelope:
        return self._event("run.failed", {"error": error, "traceback": traceback})

    def emit_run_cancelled(self) -> EventEnvelope:
        return self._event("run.cancelled", {})

    def emit_run_final(self, *, decision_text: Optional[str], rating: Optional[str]) -> EventEnvelope:
        return self._event("run.final", {"decision_text": decision_text, "rating": rating})

    def emit_heartbeat(self) -> EventEnvelope:
        return self._event("heartbeat", {})

    def handle_chunk(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        """Yield one or more events for the given chunk."""
        # Update accumulated state — last value wins for each key.
        for key, val in chunk.items():
            if val is not None:
                self.final_state[key] = val

        yield from self._yield_message_events(chunk)
        yield from self._yield_analyst_events(chunk)
        yield from self._yield_research_events(chunk)
        yield from self._yield_trader_events(chunk)
        yield from self._yield_risk_events(chunk)

    # ---------- chunk handlers ----------

    def _yield_message_events(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        for message in chunk.get("messages", []) or []:
            msg_id = getattr(message, "id", None)
            if msg_id is not None:
                if msg_id in self._processed_message_ids:
                    continue
                self._processed_message_ids.add(msg_id)

            # Tool calls are the most actionable signal for the UI ticker.
            tool_calls = getattr(message, "tool_calls", None) or []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                else:
                    name = getattr(tc, "name", "")
                    args = getattr(tc, "args", {})
                yield self._event("tool.called", {"name": str(name), "args": _safe_json(args)})

            # Surface message *content* (separate from tool calls) so the UI
            # can show agent reasoning in the "Messages & Tools" panel —
            # mirrors the Rich CLI's message stream. We truncate to keep SSE
            # payloads light; the frontend can request the full run detail if
            # it needs the unabridged text.
            content = _extract_text(getattr(message, "content", None))
            if content and not tool_calls:
                role = _classify_role(message)
                yield self._event(
                    "message",
                    {
                        "role": role,
                        "content": content if len(content) <= 600 else content[:600] + "…",
                        "full_length": len(content),
                    },
                )

    def _yield_analyst_events(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        """Mirror ``cli/main.py:update_analyst_statuses``."""
        first_pending: Optional[str] = None
        for analyst_key in self.selected_analysts:
            report_key = ANALYST_REPORT_MAP[analyst_key]
            content = chunk.get(report_key)

            if content:
                # Newly-arrived report. Emit the report event + completion.
                yield self._event(
                    "analyst.report",
                    {"analyst": analyst_key, "section": report_key, "content": content},
                )
                if analyst_key not in self._analyst_completed:
                    self._analyst_completed.add(analyst_key)
                    yield self._event("analyst.completed", {"analyst": analyst_key})
                continue

            # No report yet for this analyst — first such one is the active analyst.
            if analyst_key not in self._analyst_completed:
                if first_pending is None:
                    first_pending = analyst_key
                    if analyst_key not in self._announced_started:
                        self._announced_started.add(analyst_key)
                        yield self._event("analyst.started", {"analyst": analyst_key})

    def _yield_research_events(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        debate = chunk.get("investment_debate_state")
        if not debate:
            return

        bull_hist = (debate.get("bull_history") or "").strip()
        bear_hist = (debate.get("bear_history") or "").strip()
        judge = (debate.get("judge_decision") or "").strip()

        if (bull_hist or bear_hist) and not self._research_team_started:
            self._research_team_started = True
            yield self._event("team.started", {"team": "research"})

        for role, full_text, key in (
            ("bull", bull_hist, "investment.bull"),
            ("bear", bear_hist, "investment.bear"),
        ):
            new_text = self._take_new(key, full_text)
            if new_text:
                yield self._event(
                    "debate.update",
                    {"team": "investment", "role": role, "delta": new_text, "full": full_text},
                )

        if judge and not self._research_team_completed:
            self._research_team_completed = True
            yield self._event(
                "debate.update",
                {"team": "investment", "role": "judge", "delta": judge, "full": judge},
            )
            yield self._event("team.completed", {"team": "research"})

    def _yield_trader_events(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        plan = chunk.get("trader_investment_plan")
        if plan and not self._trader_started:
            self._trader_started = True
            yield self._event("team.started", {"team": "trading"})
        if plan and not self._trader_completed:
            self._trader_completed = True
            yield self._event(
                "report.section",
                {"section": "trader_investment_plan", "content": plan},
            )
            yield self._event("team.completed", {"team": "trading"})

    def _yield_risk_events(self, chunk: Dict[str, Any]) -> Iterator[EventEnvelope]:
        risk = chunk.get("risk_debate_state")
        if not risk:
            return

        agg_hist = (risk.get("aggressive_history") or "").strip()
        con_hist = (risk.get("conservative_history") or "").strip()
        neu_hist = (risk.get("neutral_history") or "").strip()
        judge = (risk.get("judge_decision") or "").strip()

        if (agg_hist or con_hist or neu_hist) and "risk" not in self._risk_started:
            self._risk_started.add("risk")
            yield self._event("team.started", {"team": "risk"})

        for role, full_text, key in (
            ("aggressive", agg_hist, "risk.aggressive"),
            ("conservative", con_hist, "risk.conservative"),
            ("neutral", neu_hist, "risk.neutral"),
        ):
            new_text = self._take_new(key, full_text)
            if new_text:
                yield self._event(
                    "debate.update",
                    {"team": "risk", "role": role, "delta": new_text, "full": full_text},
                )

        if judge and not self._portfolio_completed:
            self._portfolio_completed = True
            yield self._event(
                "debate.update",
                {"team": "risk", "role": "portfolio_manager", "delta": judge, "full": judge},
            )
            yield self._event(
                "report.section",
                {"section": "final_trade_decision", "content": judge},
            )
            yield self._event("team.completed", {"team": "risk"})

    # ---------- utilities ----------

    def _event(self, type: str, data: Dict[str, Any]) -> EventEnvelope:
        from datetime import datetime, timezone

        self._seq += 1
        return EventEnvelope(seq=self._seq, ts=datetime.now(timezone.utc), type=type, data=data)

    def _take_new(self, key: str, full_text: str) -> str:
        prior = self._debate_lengths.get(key, 0)
        if len(full_text) <= prior:
            return ""
        self._debate_lengths[key] = len(full_text)
        return full_text[prior:]


def _safe_json(value: Any) -> Any:
    """Coerce arbitrary tool-call args into JSON-serialisable form."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    return str(value)


def _extract_text(content: Any) -> str:
    """LangChain messages put text in either a string or a list of blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # block dicts: {"type": "text", "text": "..."}
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
        return " ".join(p.strip() for p in parts if p.strip())
    return str(content).strip()


def _classify_role(message: Any) -> str:
    """Map LangChain message class -> UI role label."""
    cls = type(message).__name__
    return {
        "AIMessage":     "Reasoning",
        "AIMessageChunk":"Reasoning",
        "HumanMessage":  "User",
        "SystemMessage": "System",
        "ToolMessage":   "Tool Result",
        "FunctionMessage":"Tool Result",
    }.get(cls, "Agent")
