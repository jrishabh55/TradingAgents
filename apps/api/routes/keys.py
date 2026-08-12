"""BYOC key management for the Gemini provider.

Routes:
  GET    /api/keys/gemini   status — manual key, OAuth availability, active source
  PUT    /api/keys/gemini   verify + encrypt + store a pasted key
  DELETE /api/keys/gemini   remove the stored key

The key is write-only from the client's perspective: responses carry at most
its last 4 characters. All handlers are sync defs on purpose — they do
blocking urllib calls (Clerk, Gemini probe) and FastAPI runs them in the
threadpool.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api import clerk_users, user_keys
from apps.api.auth import current_user_id

router = APIRouter()


class GeminiKeyBody(BaseModel):
    api_key: str = Field(..., min_length=8, description="Gemini API key (AIza…).")


class GeminiKeyStatus(BaseModel):
    manual_key: bool
    last4: str | None = None
    oauth_available: bool
    oauth_ok: bool
    oauth_error: str | None = None
    #: What a run would actually use right now. None → runs are blocked.
    active_source: str | None = None  # "manual" | "oauth" | None


@router.get("/keys/gemini", response_model=GeminiKeyStatus)
def gemini_key_status(user_id: str = Depends(current_user_id)) -> GeminiKeyStatus:
    manual = user_keys.load_gemini_key(user_id)
    token = (
        clerk_users.get_google_oauth_token(user_id) if user_keys.OAUTH_ENABLED else None
    )
    oauth_ok, oauth_error = False, None
    # Skip the Google round-trip when a manual key exists — it wins anyway.
    if token and not manual:
        oauth_ok, message = user_keys.verify_gemini(oauth_token=token)
        oauth_error = None if oauth_ok else message
    return GeminiKeyStatus(
        manual_key=bool(manual),
        last4=manual[-4:] if manual else None,
        oauth_available=token is not None,
        oauth_ok=oauth_ok,
        oauth_error=oauth_error,
        active_source="manual" if manual else ("oauth" if oauth_ok else None),
    )


@router.put("/keys/gemini", response_model=GeminiKeyStatus)
def save_gemini_key(
    body: GeminiKeyBody, user_id: str = Depends(current_user_id)
) -> GeminiKeyStatus:
    if not user_keys.encryption_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "key storage is not configured on this server "
                f"({user_keys.ENCRYPTION_KEY_ENV} unset) — ask the admin"
            ),
        )
    key = body.api_key.strip()
    ok, message = user_keys.verify_gemini(api_key=key)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    user_keys.save_gemini_key(user_id, key)
    return GeminiKeyStatus(
        manual_key=True,
        last4=key[-4:],
        oauth_available=False,  # not re-probed; manual key wins regardless
        oauth_ok=False,
        active_source="manual",
    )


@router.delete("/keys/gemini")
def delete_gemini_key(user_id: str = Depends(current_user_id)) -> dict:
    return {"deleted": user_keys.delete_gemini_key(user_id)}
