"""REST endpoints for analysis runs.

Routes:
  POST /api/runs                       create + enqueue (or return cached)
  GET  /api/runs                       list
  GET  /api/runs/{run_id}              detail
  POST /api/runs/{run_id}/cancel       request cooperative cancel
  GET  /api/runs/{run_id}/report.md    download as markdown
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from apps.api.auth import current_user_id
from apps.api.jobs.markdown import render_markdown_report
from apps.api.jobs.runner import get_runner
from apps.api.jobs.store import get_store
from apps.api.schemas import RunDetail, RunRequest, RunSummary, request_hash


router = APIRouter()


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
    user_id: str = Depends(current_user_id),
) -> RunDetail:
    """Create + enqueue a run, or return a cached completed run.

    When a completed run exists for the same canonicalized request within
    ``WEBAPP_CACHE_TTL_SECONDS``, returns it with HTTP 200 and ``cached=true``
    instead of starting a fresh pipeline. Pass ``?force=true`` to bypass.

    On a cache miss, the pipeline is enqueued and the new run row is returned
    with HTTP 201. The new run is owned by ``user_id`` (resolved by the auth
    middleware).
    """
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

    # Active-run guard is per-user: User A starting a TSLA run no longer
    # blocks User B from running TSLA. Same-user same-ticker double-submits
    # are still rejected so the user doesn't accidentally race themselves.
    if store.has_active_run_for_ticker(request.ticker, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run for {request.ticker} is already queued or running for this user. "
                   "Wait for it to finish before starting another.",
        )

    run_id = store.create_run(
        ticker=request.ticker,
        analysis_date=request.analysis_date,
        config=request.model_dump(),
        request_hash=req_hash,
        user_id=user_id,
    )
    get_runner().submit(run_id, request)

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
