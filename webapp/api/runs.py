"""REST endpoints for analysis runs.

Routes:
  POST /api/runs                       create + enqueue
  GET  /api/runs                       list
  GET  /api/runs/{run_id}              detail
  POST /api/runs/{run_id}/cancel       request cooperative cancel
  GET  /api/runs/{run_id}/report.md    download as markdown
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import List

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from webapp.jobs.markdown import render_markdown_report
from webapp.jobs.runner import get_runner
from webapp.jobs.store import get_store
from webapp.schemas import RunDetail, RunRequest, RunSummary


router = APIRouter()


@router.post("/runs", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
def create_run(request: RunRequest) -> RunDetail:
    # Validate analysis_date — cheap, would otherwise fail deep inside the graph.
    try:
        d = datetime.strptime(request.analysis_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="analysis_date must be YYYY-MM-DD")
    if d > datetime.now().date():
        raise HTTPException(status_code=400, detail="analysis_date cannot be in the future")

    store = get_store()
    if store.has_active_run_for_ticker(request.ticker):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run for {request.ticker} is already queued or running. "
                   "Wait for it to finish to avoid racing the memory log.",
        )

    run_id = store.create_run(
        ticker=request.ticker,
        analysis_date=request.analysis_date,
        config=request.model_dump(),
    )
    get_runner().submit(run_id, request)

    detail = store.get_run(run_id)
    if detail is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Run was created but immediately disappeared")
    return detail


@router.get("/runs", response_model=List[RunSummary])
def list_runs(limit: int = 100) -> List[RunSummary]:
    return get_store().list_runs(limit=limit)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    detail = get_store().get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return detail


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_run(run_id: str) -> dict:
    detail = get_store().get_run(run_id)
    if detail is None:
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
def download_report(run_id: str) -> StreamingResponse:
    detail = get_store().get_run(run_id)
    if detail is None:
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
