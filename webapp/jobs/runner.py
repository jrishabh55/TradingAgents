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
runs. Default concurrency is 1 (env ``WEBAPP_CONCURRENCY``) — running two
graphs in parallel would race the on-disk memory log (see FORK_PATCHES.md and
the architecture plan's "Risks" section).
"""
from __future__ import annotations

import logging
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from webapp.jobs.bus import EventBus, get_bus
from webapp.jobs.markdown import render_markdown_report
from webapp.jobs.store import JobStore, get_store
from webapp.jobs.translator import ChunkTranslator
from webapp.schemas import RunRequest


logger = logging.getLogger(__name__)


class _CancelToken:
    """Cooperative cancellation flag, polled between graph chunks."""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def is_cancelled(self) -> bool:
        return self._flag.is_set()


class JobRunner:
    """Owns the worker pool and the live cancellation tokens."""

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

    def submit(self, run_id: str, request: RunRequest) -> None:
        """Hand the run off to the worker pool. Returns immediately."""
        token = _CancelToken()
        with self._tokens_lock:
            self._cancel_tokens[run_id] = token
        self.executor.submit(self._run_safely, run_id, request, token)

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

    def _run_safely(self, run_id: str, request: RunRequest, token: _CancelToken) -> None:
        try:
            self._run(run_id, request, token)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("Runner crashed for run %s", run_id)
            self._fail(run_id, repr(exc), traceback.format_exc())
        finally:
            with self._tokens_lock:
                self._cancel_tokens.pop(run_id, None)

    def _run(self, run_id: str, request: RunRequest, token: _CancelToken) -> None:
        # Local imports keep the module importable in tests without the upstream
        # graph package fully resolved.
        from webapp.integrations.graph_factory import build_graph_for_request
        from tradingagents.agents.utils.rating import parse_rating

        translator = ChunkTranslator(run_id, selected_analysts=request.analysts)

        # Build graph + initial state. This may raise if config is invalid; it'll
        # propagate up to _run_safely and become a run.failed event.
        graph, init_state, args, stats_handler = build_graph_for_request(request)

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

        try:
            for chunk in graph.graph.stream(init_state, **args):
                if token.is_cancelled():
                    self._cancelled(run_id, translator)
                    return
                for env in translator.handle_chunk(chunk):
                    self._publish(run_id, env)
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

    def _fail(self, run_id: str, error: str, tb: Optional[str]) -> None:
        self.store.mark_failed(run_id, error)
        # Use a fresh translator if none exists; we just need a seq for the failure event.
        # The bus will be a no-op if nobody's listening.
        from datetime import datetime, timezone

        from webapp.schemas import EventEnvelope

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
        concurrency = int(os.environ.get("WEBAPP_CONCURRENCY", "1"))
        _runner = JobRunner(store=get_store(), bus=get_bus(), concurrency=concurrency)
    return _runner


def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        _runner.shutdown()
        _runner = None
