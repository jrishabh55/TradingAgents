"""REST endpoints for analysis runs.

Routes:
  POST /api/runs                       create + enqueue (or return cached)
  GET  /api/runs                       list
  GET  /api/runs/{run_id}              detail
  POST /api/runs/{run_id}/cancel       request cooperative cancel
  GET  /api/runs/{run_id}/report.md    download as markdown
  GET  /api/runs/{run_id}/levels       computed stop / target / position size
  POST /api/runs/{run_id}/resume       continue an interrupted run
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse

from apps.api import clerk_users
from apps.api.auth import ANONYMOUS_USER_ID, SHARED_BEARER_USER_ID, current_user_id
from apps.api.jobs.markdown import render_markdown_report
from apps.api.jobs.runner import get_runner
from apps.api.jobs.store import get_store
from apps.api.schemas import (
    LevelsResponse,
    RunDetail,
    RunRequest,
    RunSummary,
    request_hash,
)


router = APIRouter()

#: The product's provider surface: OpenAI on the server key (paid in credits),
#: the ChatGPT-subscription helper, and Gemini on the user's own credential.
ALLOWED_PROVIDERS = {"openai", "google", "chatgpt_helper"}


def _user_owns(detail: RunDetail, user_id: str) -> bool:
    """Whether ``detail`` belongs to ``user_id``.

    Pre-auth runs (no user_id) are owned by the synthetic 'anonymous' user —
    so an open deployment behaves the same as before. Once auth is enabled,
    legacy rows can be backfilled via a one-shot UPDATE.
    """
    owner = detail.user_id or "anonymous"
    return owner == user_id


def _cache_ttl_seconds() -> int:
    """How fresh a completed run must be to satisfy a cache lookup.

    Default 24h. Trading agents fetch news/sentiment data up to "now" (not
    sliced to ``analysis_date``), so an old cached report misses headlines
    that have arrived since. 24h is short enough to feel current; lower it
    via ``WEBAPP_CACHE_TTL_SECONDS`` if your data shifts faster.
    """
    try:
        return int(os.environ.get("WEBAPP_CACHE_TTL_SECONDS", str(24 * 3600)))
    except ValueError:
        return 24 * 3600


@router.post("/runs", response_model=RunDetail)
def create_run(
    request: RunRequest,
    response: Response,
    force: bool = False,
    skip_preflight: bool = Query(
        False,
        description=(
            "Enqueue even when the ticker has no resolvable price data. Separate "
            "from `force` on purpose — `force` bypasses the result cache, which "
            "must not also disable data validation."
        ),
    ),
    user_id: str = Depends(current_user_id),
) -> RunDetail:
    """Create + enqueue a run, or return a cached completed run.

    When a completed run exists for the same canonicalized request within
    ``WEBAPP_CACHE_TTL_SECONDS``, returns it with HTTP 200 and ``cached=true``
    instead of starting a fresh pipeline. Pass ``?force=true`` to bypass.

    The ticker is resolved against the price vendor before anything is enqueued;
    a symbol with no OHLCV is rejected with HTTP 400 and did-you-mean
    suggestions rather than spending a full pipeline producing a rating with no
    data behind it. ``?skip_preflight=true`` overrides.

    On a cache miss, the pipeline is enqueued and the new run row is returned
    with HTTP 201. The new run is owned by ``user_id`` (resolved by the auth
    middleware).
    """
    # Only the three product providers are accepted. Server-side on purpose:
    # trimming the UI dropdown alone would still let a direct API call run
    # e.g. "anthropic" against whatever server-side env keys exist.
    provider = (request.llm_provider or "").strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported llm_provider '{request.llm_provider}' — "
                   f"use one of: {', '.join(sorted(ALLOWED_PROVIDERS))}",
        )

    # Validate analysis_date — cheap, would otherwise fail deep inside the graph.
    try:
        d = datetime.strptime(request.analysis_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="analysis_date must be YYYY-MM-DD")
    if d > datetime.now().date():
        raise HTTPException(status_code=400, detail="analysis_date cannot be in the future")

    store = get_store()
    req_hash = request_hash(request)

    # Cache lookup before submitting work. Shared cache across users — report
    # content is a public-data analysis, not user-specific. To make the cache
    # per-user, pass user_id=user_id below.
    if not force:
        cached = store.find_cached_run(req_hash, ttl_seconds=_cache_ttl_seconds())
        if cached is not None:
            cached.cached = True
            response.status_code = status.HTTP_200_OK
            return cached

    # A helper-backed run with no helper anywhere (no live loopback on this
    # host, this user's helper not on the relay) is guaranteed to fail deep in
    # the pipeline — reject it here with the fix instead. AFTER the cache
    # lookup on purpose: a cached answer needs no helper.
    from apps.api.integrations.helper_backend import helper_ready, is_helper_provider

    if is_helper_provider(request.llm_provider) and not helper_ready(user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "your helper is not connected — open the helper app on your "
                "machine (or install it first), then retry"
            ),
        )

    # Same idea for Gemini BYOC: a run with no user credential fails at graph
    # construction, so reject it here with the fix instead.
    if provider == "google":
        from apps.api import user_keys

        if not user_keys.gemini_credential_available(user_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Gemini runs use your own API key — paste one in the "
                    "model settings"
                ),
            )

    # Resolve prices before spending anything. Runs after the cache lookup so a
    # cache hit stays instant, and before the active-run guard so a typo can't
    # occupy the per-user ticker slot. On success this warms the OHLCV cache the
    # market analyst is about to read.
    if not skip_preflight:
        from apps.api.integrations.preflight import preflight_ticker

        check = preflight_ticker(request.ticker, request.analysis_date)
        if not check.ok:
            # Structured so the UI can offer the suggestions as one-tap fixes
            # instead of regexing them back out of a sentence.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "no_market_data",
                    "message": check.as_error_detail(),
                    "ticker": check.ticker,
                    "suggestions": check.suggestions,
                },
            )

    # Active-run guard is per-user: User A starting a TSLA run no longer
    # blocks User B from running TSLA. Same-user same-ticker double-submits
    # are still rejected so the user doesn't accidentally race themselves.
    if store.has_active_run_for_ticker(request.ticker, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run for {request.ticker} is already queued or running for this user. "
                   "Wait for it to finish before starting another.",
        )

    # Credits: 1 per fresh OPENAI pipeline run, debited last on purpose — cache
    # hits, validation failures, helper 409s, and preflight rejections above
    # never cost anything. Helper and Gemini BYOC runs are free: the user's own
    # subscription/credential pays the LLM bill, not the server's key. Resume
    # stays free (it's failure recovery, already capped by MAX_RESUMES).
    # Synthetic users (legacy bearer / open mode) have no Clerk record, so the
    # gate only applies to real Clerk users.
    if (
        provider == "openai"
        and clerk_users.enabled()
        and user_id not in (ANONYMOUS_USER_ID, SHARED_BEARER_USER_ID)
    ):
        try:
            remaining = clerk_users.debit_credit(user_id)
        except HTTPException:
            raise
        except Exception:
            # Clerk API unreachable: fail closed on spend, but as a retriable
            # 503 rather than a misleading "out of credits".
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="credit check unavailable, try again shortly",
            )
        if remaining is None:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "insufficient_credits",
                    "message": "You're out of credits — ask the admin for a top-up.",
                    "credits": 0,
                },
            )

    run_id = store.create_run(
        ticker=request.ticker,
        analysis_date=request.analysis_date,
        config=request.model_dump(),
        request_hash=req_hash,
        user_id=user_id,
    )
    get_runner().submit(run_id, request, user_id=user_id)

    detail = store.get_run(run_id)
    if detail is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Run was created but immediately disappeared")
    response.status_code = status.HTTP_201_CREATED
    return detail


@router.get("/runs", response_model=List[RunSummary])
def list_runs(
    limit: int = 100,
    user_id: str = Depends(current_user_id),
) -> List[RunSummary]:
    return get_store().list_runs(limit=limit, user_id=user_id)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    user_id: str = Depends(current_user_id),
) -> RunDetail:
    detail = get_store().get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Don't leak other users' runs. 404 (not 403) so the response doesn't
    # confirm a run by this id exists for someone else.
    if not _user_owns(detail, user_id):
        raise HTTPException(status_code=404, detail="run not found")
    return detail


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_run(
    run_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    detail = get_store().get_run(run_id)
    if detail is None or not _user_owns(detail, user_id):
        raise HTTPException(status_code=404, detail="run not found")
    if detail.status not in ("queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is already {detail.status}",
        )
    accepted = get_runner().cancel(run_id)
    if not accepted:
        # Token already cleared (run finished between status check and cancel).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is no longer cancellable",
        )
    return {"run_id": run_id, "cancel_requested": True}


@router.get("/runs/{run_id}/report.md", response_class=StreamingResponse)
def download_report(
    run_id: str,
    user_id: str = Depends(current_user_id),
) -> StreamingResponse:
    detail = get_store().get_run(run_id)
    if detail is None or not _user_owns(detail, user_id):
        raise HTTPException(status_code=404, detail="run not found")

    md = _render_markdown(detail)
    filename = f"{detail.ticker}_{detail.analysis_date}.md"
    return StreamingResponse(
        io.BytesIO(md.encode("utf-8")),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/runs/{run_id}/resume", response_model=RunDetail)
def resume_run(
    run_id: str,
    user_id: str = Depends(current_user_id),
) -> RunDetail:
    """Continue an interrupted run from its last completed node.

    Only ``interrupted`` runs are resumable — a failed run errored and would
    error again, and a completed one has nothing to do. The claim is an atomic
    ``interrupted -> running`` UPDATE, so two concurrent resume requests cannot
    both start the graph against the same checkpoint.
    """
    store = get_store()
    detail = store.get_run(run_id)
    if detail is None or not _user_owns(detail, user_id):
        raise HTTPException(status_code=404, detail="run not found")
    if detail.status != "interrupted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is {detail.status}; only interrupted runs can be resumed",
        )
    if store.get_checkpoint_context(run_id) is None:
        # Without a checkpoint there is nothing to continue from; resuming would
        # silently restart the whole pipeline and bill for it again.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this run has no checkpoint, so it cannot be resumed — it was "
                "started with checkpoint_enabled=false. Start a new run instead."
            ),
        )

    # Check helper availability BEFORE consuming the atomic claim: a claim that
    # then fails graph construction marks the run `failed` permanently, turning
    # a transiently-disconnected helper into a lost run.
    from apps.api.integrations.helper_backend import helper_ready, is_helper_provider

    if is_helper_provider(detail.config.get("llm_provider", "")) and not helper_ready(user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "your helper is not connected — open the helper app on your "
                "machine, wait for it to connect, then resume"
            ),
        )

    if not store.claim_for_resume(run_id, user_id=user_id):
        # Either someone else won the race between our status read and the
        # claim, or the run has hit the resume cap (each resume re-bills the
        # LLM calls after the last checkpoint, so poison runs must not loop).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is already being resumed, or has been resumed too many times",
        )

    request = RunRequest(**detail.config)
    get_runner().submit(run_id, request, user_id=user_id, resume=True)

    resumed = store.get_run(run_id)
    return resumed if resumed is not None else detail


@router.get("/runs/{run_id}/levels", response_model=LevelsResponse)
def get_run_levels(
    run_id: str,
    capital: float = Query(
        ...,
        gt=0,
        description=(
            "Account capital, expressed in the INSTRUMENT'S quote currency "
            "(see `currency` in the response). No FX conversion is performed."
        ),
    ),
    risk_pct: float = Query(
        1.0, gt=0, le=100, description="Percent of capital risked on this trade (1-2 typical)."
    ),
    r_multiple: float = Query(
        2.0, ge=1, le=10, description="Primary target as a multiple of the stop distance."
    ),
    user_id: str = Depends(current_user_id),
) -> LevelsResponse:
    """Structure-based stop, risk-multiple target, and fixed-fractional size.

    Computed on demand rather than frozen at run time: it's deterministic
    arithmetic over cached OHLCV, so changing ``capital`` or ``risk_pct``
    re-sizes instantly without re-running the (expensive) agent pipeline.
    Levels are derived as of the run's ``analysis_date``, keeping them
    consistent with the report.

    Long side only. A short-implying rating comes back ``viable=false``.
    """
    # Local import: pulls pandas/stockstats, and keeps module import cheap for
    # tests that never touch levels.
    from apps.api.integrations.levels import LevelsUnavailable, compute_levels

    detail = get_store().get_run(run_id)
    if detail is None or not _user_owns(detail, user_id):
        raise HTTPException(status_code=404, detail="run not found")
    if detail.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is {detail.status}; levels need a completed run",
        )

    try:
        return compute_levels(
            run_id=run_id,
            ticker=detail.ticker,
            analysis_date=detail.analysis_date,
            capital=capital,
            risk_pct=risk_pct,
            r_multiple=r_multiple,
            rating=detail.rating,
            trader_plan=detail.trader_investment_plan,
        )
    except LevelsUnavailable as exc:
        # The run itself is fine — the market data can't support the maths.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _render_markdown(detail: RunDetail) -> str:
    """Build the Markdown shape, sourcing fields from a RunDetail."""
    return render_markdown_report(
        ticker=detail.ticker,
        analysis_date=detail.analysis_date,
        rating=detail.rating,
        final_state={
            "market_report": detail.market_report,
            "sentiment_report": detail.sentiment_report,
            "news_report": detail.news_report,
            "fundamentals_report": detail.fundamentals_report,
            "investment_debate_state": detail.investment_debate_state,
            "trader_investment_plan": detail.trader_investment_plan,
            "risk_debate_state": detail.risk_debate_state,
            "final_trade_decision": detail.final_trade_decision,
        },
    )
