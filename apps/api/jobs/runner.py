"""Background worker that drives one ``TradingAgentsGraph`` run.

Lifecycle:
    queued  → mark_running → run.started event
            → for chunk in graph.graph.stream(...):
                  translator.handle_chunk → persist + publish events
                  check cancel flag
            → graph.process_signal → parse_rating
            → mark_completed → run.final event
    failure  → mark_failed → run.failed event
    cancel   → mark_cancelled → run.cancelled event

Threading: a single ``ThreadPoolExecutor`` shared by the FastAPI app submits
runs. ``WEBAPP_CONCURRENCY`` controls the pool size (default 4). Concurrent
parallelism is safe across users — each user has a private memory log path
injected by ``apps/api/integrations/graph_factory.py``. Same-user runs are
serialized through a per-user lock taken inside ``_run_safely`` so two graphs
from the same user can't race their own log.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from apps.api.jobs.bus import EventBus, get_bus
from apps.api.jobs.markdown import render_markdown_report
from apps.api.jobs.store import JobStore, get_store
from apps.api.jobs.translator import ChunkTranslator
from apps.api.schemas import RunRequest


logger = logging.getLogger(__name__)


# Tools whose failure means the run has no prices to reason about. Fundamentals
# are deliberately absent: an index or commodity legitimately has none, and
# news/sentiment already degrade to sentinels inside the core.
_PRICE_TOOLS = {"get_stock_data", "get_indicators", "get_verified_market_snapshot"}

# Literal prefix the core's vendor router returns for a core category with no
# usable data (tradingagents/dataflows/interface.py). Matching the TOOL RESULT
# rather than the analyst's report is deliberate: the report is LLM prose and
# would be written in the run's `output_language`, so any string match against it
# breaks the moment someone runs in Hindi or Chinese.
_NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"


def find_price_data_failure(chunk: Dict[str, Any]) -> Optional[str]:
    """Return a reason string when a price tool reported no usable data.

    Scans the chunk's ToolMessages for the core's no-data sentinel. Returns None
    when prices are fine, so the caller can treat this as a cheap per-chunk gate.
    """
    for message in chunk.get("messages", []) or []:
        name = str(getattr(message, "name", "") or "")
        if name not in _PRICE_TOOLS:
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        if content.lstrip().startswith(_NO_DATA_SENTINEL):
            # Keep the core's own detail (invalid symbol / stale / not covered).
            return f"{name}: {content.strip()[:400]}"
    return None


def _prior_state(detail) -> Optional[Dict[str, Any]]:
    """The report sections an interrupted attempt already produced."""
    if detail is None:
        return None
    keys = (
        "market_report", "sentiment_report", "news_report", "fundamentals_report",
        "investment_plan", "trader_investment_plan", "final_trade_decision",
        "investment_debate_state", "risk_debate_state",
    )
    return {k: v for k in keys if (v := getattr(detail, k, None)) is not None}


class _CancelToken:
    """Cooperative cancellation flag, polled between graph chunks."""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def is_cancelled(self) -> bool:
        return self._flag.is_set()


class JobRunner:
    """Owns the worker pool, per-user serialization locks, and cancel tokens."""

    def __init__(
        self,
        *,
        store: JobStore,
        bus: EventBus,
        concurrency: int = 1,
    ) -> None:
        self.store = store
        self.bus = bus
        self.executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="ta-runner"
        )
        self._cancel_tokens: Dict[str, _CancelToken] = {}
        self._tokens_lock = threading.Lock()
        # Per-user mutex so same-user concurrent runs serialize on the
        # (user-scoped) memory log. Different users hit different locks and
        # run in parallel.
        self._user_locks: Dict[str, threading.Lock] = {}
        self._user_locks_lock = threading.Lock()
        # run_id -> graph, so the finally block can close its checkpointer.
        self._graphs: Dict[str, Any] = {}

    def _get_user_lock(self, user_id: str) -> threading.Lock:
        """Return the (lazily-created) lock for ``user_id``."""
        with self._user_locks_lock:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[user_id] = lock
            return lock

    def submit(
        self,
        run_id: str,
        request: RunRequest,
        *,
        user_id: str = "anonymous",
        resume: bool = False,
    ) -> None:
        """Hand the run off to the worker pool. Returns immediately.

        ``resume=True`` continues an interrupted run from its last checkpoint
        instead of starting the graph over. The caller must already have won
        ``store.claim_for_resume`` — this method does not arbitrate.
        """
        token = _CancelToken()
        with self._tokens_lock:
            self._cancel_tokens[run_id] = token
        self.executor.submit(self._run_safely, run_id, request, token, user_id, resume)

    def cancel(self, run_id: str) -> bool:
        with self._tokens_lock:
            token = self._cancel_tokens.get(run_id)
        if token is None:
            return False
        token.cancel()
        return True

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------- worker entry point ----------

    def _run_safely(
        self,
        run_id: str,
        request: RunRequest,
        token: _CancelToken,
        user_id: str,
        resume: bool = False,
    ) -> None:
        try:
            # Per-user lock: blocks only OTHER runs from the SAME user. Held
            # for the entire pipeline so the memory log read-modify-write is
            # serialized within a user. Different users hit different locks.
            with self._get_user_lock(user_id):
                self._run(run_id, request, token, user_id, resume=resume)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("Runner crashed for run %s", run_id)
            self._fail(run_id, repr(exc), traceback.format_exc())
        finally:
            with self._tokens_lock:
                self._cancel_tokens.pop(run_id, None)
            # The SqliteSaver context is opened in graph_factory and parked on the
            # graph. Close it on EVERY exit path — error, cancel, early return —
            # or the handle leaks for the process lifetime.
            self._close_checkpointer(run_id)

    def _run(
        self,
        run_id: str,
        request: RunRequest,
        token: _CancelToken,
        user_id: str,
        resume: bool = False,
    ) -> None:
        # Local imports keep the module importable in tests without the upstream
        # graph package fully resolved.
        from apps.api.integrations.graph_factory import (
            build_graph_for_request,
            effective_analysts,
        )
        from tradingagents.agents.utils.rating import parse_rating

        # Use the post-filter analyst list, not request.analysts: on a crypto
        # ticker the Fundamentals analyst is dropped, and the translator's list
        # is what run.started reports to the frontend. Passing the unfiltered
        # list would leave the UI waiting on a panel that never emits.
        #
        # On resume, seed the sequence counter and the accumulated state from
        # storage. A fresh translator would restart seq at 0 and collide with the
        # events already persisted for this run (UNIQUE(run_id, seq)), and would
        # re-emit sections the interrupted attempt had already produced.
        prior_detail = self.store.get_run(run_id) if resume else None
        translator = ChunkTranslator(
            run_id,
            selected_analysts=effective_analysts(request),
            start_seq=self.store.latest_seq(run_id) if resume else 0,
            replay_state=_prior_state(prior_detail) if resume else None,
        )

        # Build graph + initial state with per-user memory log. This may raise
        # if config is invalid; it'll propagate up to _run_safely and become
        # a run.failed event.
        graph, init_state, args, stats_handler = build_graph_for_request(
            request, user_id=user_id, run_id=run_id
        )
        # Record what a resume needs BEFORE any work happens: the namespace, and
        # the effective config the graph actually launched with (config_json holds
        # the request, and the graph re-applies env-derived defaults on top).
        self._graphs[run_id] = graph
        ns = (args.get("config", {}).get("configurable", {}) or {}).get("thread_id")
        if resume:
            # This comparison is the whole reason effective_config is persisted:
            # the graph re-applies env-derived defaults at build time, so if the
            # server's env drifted since the run started, resuming would silently
            # continue under settings the run never launched with. Refuse instead.
            # Round-trip through JSON so both sides are normalised identically.
            stored = self.store.get_checkpoint_context(run_id) or {}
            expected = stored.get("effective_config") or None
            current = json.loads(
                json.dumps(_redact_config(dict(graph.config)), sort_keys=True, default=str)
            )
            if expected and current != expected:
                self._fail(
                    run_id,
                    "server configuration changed since this run started; "
                    "resuming under different settings is unsafe — start a new run",
                    None,
                )
                return
        elif ns:
            self.store.set_checkpoint_context(
                run_id,
                checkpoint_ns=ns,
                effective_config=_redact_config(dict(graph.config)),
            )

        self.store.mark_running(run_id)
        self._publish(
            run_id,
            translator.emit_run_started(
                ticker=request.ticker,
                analysis_date=request.analysis_date,
                config=_redact_config(request.model_dump()),
            ),
        )

        if token.is_cancelled():
            self._cancelled(run_id, translator)
            return

        # LangGraph resumes only when invoked with input=None: the checkpointer
        # supplies the state. Resending init_state would start the graph again
        # from the top, which is exactly what resume exists to avoid.
        stream_input = None if resume else init_state
        try:
            for chunk in graph.graph.stream(stream_input, **args):
                if token.is_cancelled():
                    self._cancelled(run_id, translator)
                    return
                for env in translator.handle_chunk(chunk):
                    self._publish(run_id, env)

                # Fail fast when prices are unavailable. Preflight catches a bad
                # symbol at submit time; this catches a vendor that broke between
                # submit and the market analyst's call. Breaking the loop
                # abandons the generator, so the remaining agents never run —
                # the alternative is paying for a debate over nothing and
                # emitting a rating with no data behind it.
                reason = find_price_data_failure(chunk)
                if reason is not None:
                    logger.warning("Run %s aborted — no price data. %s", run_id, reason)
                    self._fail(
                        run_id,
                        "Market data unavailable — run stopped before the "
                        f"remaining agents ran. {reason}",
                        None,
                    )
                    return
                # Snapshot in-progress state so the download endpoint can
                # serve a partial report mid-run.
                self.store.update_final_state(run_id, translator.final_state)
                # After each chunk, snapshot stats and publish if changed.
                self._maybe_publish_stats(run_id, translator, stats_handler)
        except Exception as exc:
            logger.exception("Graph stream raised for run %s", run_id)
            self._fail(run_id, repr(exc), traceback.format_exc())
            return

        # Final state is whatever the translator accumulated.
        final_state = translator.final_state
        decision_text = final_state.get("final_trade_decision")
        rating: Optional[str] = parse_rating(decision_text) if decision_text else None

        # One last stats snapshot, then the terminal event.
        self._maybe_publish_stats(run_id, translator, stats_handler, force=True)

        # Persist the final-state snapshot + decision before emitting run.final.
        self.store.mark_completed(
            run_id,
            decision_text=decision_text,
            rating=rating,
            final_state=final_state,
        )

        # Mirror the report to disk so users have a permanent file-system
        # archive alongside SQLite. Best-effort — failures are logged but
        # don't fail the run.
        try:
            self._export_to_disk(run_id, request, final_state, rating)
        except Exception:
            logger.exception("Failed to write disk export for run %s", run_id)

        self._publish(run_id, translator.emit_run_final(decision_text=decision_text, rating=rating))

    # ---------- helpers ----------

    def _publish(self, run_id: str, env) -> None:
        self.store.append_event(
            run_id,
            seq=env.seq,
            type=env.type,
            data=env.data,
            ts=env.ts.isoformat(),
        )
        self.bus.publish(run_id, env)

    def _export_to_disk(
        self,
        run_id: str,
        request: RunRequest,
        final_state: Dict[str, Any],
        rating: Optional[str],
    ) -> None:
        """Write the rendered Markdown report to a per-run file."""
        from tradingagents.dataflows.utils import safe_ticker_component

        # Validate the ticker before using it as a path component.
        safe_ticker = safe_ticker_component(request.ticker)

        base = Path(
            os.environ.get(
                "WEBAPP_REPORTS_DIR",
                str(Path.home() / ".tradingagents" / "webapp_reports"),
            )
        )
        target = base / safe_ticker / request.analysis_date / f"{run_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        md = render_markdown_report(
            ticker=request.ticker,
            analysis_date=request.analysis_date,
            rating=rating,
            final_state=final_state,
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )
        target.write_text(md, encoding="utf-8")
        logger.info("Wrote disk export %s (%d bytes)", target, len(md))

    def _maybe_publish_stats(
        self,
        run_id: str,
        translator: ChunkTranslator,
        stats_handler,
        *,
        force: bool = False,
    ) -> None:
        """Snapshot stats handler counters and emit a stats.update event if they changed."""
        try:
            snapshot = stats_handler.get_stats()
        except Exception:  # pragma: no cover — defensive
            return
        last = getattr(self, "_last_stats", {}).get(run_id) if hasattr(self, "_last_stats") else None
        if not force and last == snapshot:
            return
        if not hasattr(self, "_last_stats"):
            self._last_stats = {}
        self._last_stats[run_id] = snapshot
        self._publish(run_id, translator._event("stats.update", snapshot))

    def _close_checkpointer(self, run_id: str) -> None:
        graph = self._graphs.pop(run_id, None)
        if graph is None:
            return
        # A relay-routed run holds a per-run internal token; a leaked token
        # must die with the run, on every exit path.
        token = getattr(graph, "_relay_token", None)
        if token:
            from apps.api.routes.relay import get_internal_tokens

            get_internal_tokens().revoke(token)
            graph._relay_token = None
        ctx = getattr(graph, "_checkpointer_ctx", None)
        if ctx is None:
            return
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — teardown must never mask the real error
            logger.debug("checkpointer teardown failed for %s", run_id, exc_info=True)
        finally:
            graph._checkpointer_ctx = None

    def _fail(self, run_id: str, error: str, tb: Optional[str]) -> None:
        self.store.mark_failed(run_id, error)
        # Use a fresh translator if none exists; we just need a seq for the failure event.
        # The bus will be a no-op if nobody's listening.
        from datetime import datetime, timezone

        from apps.api.schemas import EventEnvelope

        seq = self.store.latest_seq(run_id) + 1
        env = EventEnvelope(
            seq=seq,
            ts=datetime.now(timezone.utc),
            type="run.failed",
            data={"error": error, "traceback": tb},
        )
        self.store.append_event(
            run_id, seq=env.seq, type=env.type, data=env.data, ts=env.ts.isoformat()
        )
        self.bus.publish(run_id, env)

    def _cancelled(self, run_id: str, translator: ChunkTranslator) -> None:
        self.store.mark_cancelled(run_id)
        self._publish(run_id, translator.emit_run_cancelled())


def _redact_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any field that smells like a credential before publishing it."""
    redacted = dict(config)
    for k in list(redacted):
        lower = k.lower()
        if "key" in lower or "token" in lower or "secret" in lower:
            redacted[k] = "***redacted***"
    return redacted


# Module-level singleton tied to the FastAPI lifespan.
_runner: Optional[JobRunner] = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        # Default 10: safe across users now that each has their own memory log,
        # and the operator's OpenAI tier absorbs the parallel call volume.
        # Same-user runs are serialized by the per-user lock inside the runner.
        concurrency = int(os.environ.get("WEBAPP_CONCURRENCY", "10"))
        _runner = JobRunner(store=get_store(), bus=get_bus(), concurrency=concurrency)
    return _runner


def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        _runner.shutdown()
        _runner = None
