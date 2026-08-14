"""REST endpoints for the stock scanner.

Routes:
  GET    /api/scanners               list (prebuilt + own)
  POST   /api/scanners               create
  PUT    /api/scanners/{sid}         update own
  DELETE /api/scanners/{sid}         delete own
  POST   /api/scanners/{sid}/run     run saved scanner
  POST   /api/scanners/preview       run an unsaved definition
  POST   /api/scanners/nl            natural language -> definition
  GET    /api/scanners/status        data-freshness summary (universe + latest bar per timeframe)
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from apps.api.auth import current_user_id
from apps.api.scanner.engine import get_engine
from apps.api.scanner.schema import DefinitionError, Group, parse_definition
from apps.api.scanner.store import get_scanner_store

router = APIRouter()


class ScannerBody(BaseModel):
    name: str
    description: str = ""
    definition: Dict[str, Any]


class PreviewBody(BaseModel):
    definition: Dict[str, Any]


class NlBody(BaseModel):
    prompt: str


def _parse_or_422(definition: Dict[str, Any]) -> Group:
    try:
        return parse_definition(definition)
    except DefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[:5])


def _owned_or_error(sid: str, user_id: str, *, for_write: bool) -> Dict[str, Any]:
    scanner = get_scanner_store().get_scanner(sid)
    if scanner is None:
        raise HTTPException(status_code=404, detail="scanner not found")
    if scanner["prebuilt"]:
        if for_write:
            raise HTTPException(status_code=403, detail="prebuilt scanners are read-only")
        return scanner
    if scanner["user_id"] != user_id:
        # 404, not 403: don't leak that another user's scanner id exists.
        raise HTTPException(status_code=404, detail="scanner not found")
    return scanner


#: Timeframes surfaced on the workbench's "last data refresh" line — a
#: subset of schema.TIMEFRAMES (no 1w/1mo: those aren't shown there).
STATUS_TIMEFRAMES = ("1d", "1h", "15m", "5m")


@router.get("/scanners")
def list_scanners(user_id: str = Depends(current_user_id)) -> list:
    return get_scanner_store().list_scanners(user_id)


@router.get("/scanners/status")
def scanner_status(user_id: str = Depends(current_user_id)) -> dict:
    store = get_scanner_store()
    return {
        "universe": len(store.instruments_df()),
        "latest": {tf: store.latest_ts(tf) for tf in STATUS_TIMEFRAMES},
    }


@router.post("/scanners", status_code=201)
def create_scanner(body: ScannerBody, user_id: str = Depends(current_user_id)) -> dict:
    _parse_or_422(body.definition)
    store = get_scanner_store()
    sid = store.create_scanner(user_id, body.name, body.description, body.definition)
    return store.get_scanner(sid)


@router.put("/scanners/{sid}")
def update_scanner(sid: str, body: ScannerBody,
                   user_id: str = Depends(current_user_id)) -> dict:
    _owned_or_error(sid, user_id, for_write=True)
    _parse_or_422(body.definition)
    store = get_scanner_store()
    store.update_scanner(sid, body.name, body.description, body.definition)
    return store.get_scanner(sid)


@router.delete("/scanners/{sid}", status_code=204)
def delete_scanner(sid: str, user_id: str = Depends(current_user_id)) -> None:
    _owned_or_error(sid, user_id, for_write=True)
    get_scanner_store().delete_scanner(sid)


def _run_or_422(definition: Group) -> dict:
    try:
        return get_engine().run(definition)
    except (ValueError, KeyError, IndexError) as exc:
        # Point-fixes in engine.py/indicators.py/schema.py should prevent
        # these on validated input; this is a backstop so any residual
        # engine throw comes back as a clean 422, not a 500.
        raise HTTPException(status_code=422, detail=f"scan failed: {exc}")


@router.post("/scanners/preview")
def preview(body: PreviewBody, user_id: str = Depends(current_user_id)) -> dict:
    return _run_or_422(_parse_or_422(body.definition))


@router.post("/scanners/{sid}/run")
def run_scanner(sid: str, user_id: str = Depends(current_user_id)) -> dict:
    scanner = _owned_or_error(sid, user_id, for_write=False)
    return _run_or_422(_parse_or_422(scanner["definition"]))


@router.post("/scanners/nl")
def nl_scanner(body: NlBody, user_id: str = Depends(current_user_id)) -> dict:
    from apps.api.scanner import nl  # late import: keeps langchain out of test startup
    try:
        definition, explanation = nl.generate_definition(body.prompt)
    except nl.NlGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"definition": definition, "explanation": explanation}
