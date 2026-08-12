"""Per-user BYOC credentials for Gemini: encrypted at rest, plaintext in memory.

Two sources, resolved in this order:

1. **Manual key** — pasted in the UI, Fernet-encrypted with
   ``WEBAPP_KEY_ENCRYPTION_SECRET`` and stored in SQLite
   (``user_api_keys`` table, jobs/store.py). An explicit user action, so it
   wins — and it's the escape hatch if the OAuth token turns out not to work
   against the Gemini API.
2. **Google OAuth token** — fetched from Clerk at run time
   (clerk_users.get_google_oauth_token). The Clerk Google connection carries
   the ``cloud-platform`` scope. Passing a ``credentials`` object flips
   langchain-google-genai onto its **Vertex AI backend**, which needs a GCP
   project: ``GOOGLE_CLOUD_PROJECT`` (read natively by the google-genai
   client). The user's token must have Vertex AI access on that project —
   without the env var the OAuth path reports itself unusable instead of
   failing mid-run.

The decrypted key/token never touches ``RunRequest`` or ``config_json`` — it
is resolved at graph construction and injected via provider kwargs, the same
in-memory-only path the helper token uses (see integrations/helper_backend.py).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Master switch for the Google-OAuth (Vertex AI) path. OFF for now — the
#: product ships manual keys only; the plumbing below (Clerk token fetch,
#: Vertex probe, credentials injection) stays tested and ready to flip on.
OAUTH_ENABLED = False

ENCRYPTION_KEY_ENV = "WEBAPP_KEY_ENCRYPTION_SECRET"
#: GCP project for OAuth-token (Vertex AI) runs. The standard env var — the
#: google-genai client reads it natively when picking the Vertex project.
PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"

#: Manual keys use the Gemini Developer API.
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
#: OAuth tokens use Vertex AI — the backend langchain-google-genai selects
#: when handed a ``credentials`` object, so the probe must hit the same API.
VERTEX_COUNT_TOKENS_URL = (
    "https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/"
    "publishers/google/models/{model}:countTokens"
)
#: Cheap, current model for the probe — free call, validates auth+IAM+API.
PROBE_MODEL = "gemini-3.5-flash"

#: Row key in the user_api_keys table.
GEMINI_PROVIDER = "gemini"


def _fernet():
    """The Fernet instance, or None when no encryption secret is configured."""
    secret = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet  # via pyjwt[crypto]'s cryptography

    return Fernet(secret.encode())


def encryption_enabled() -> bool:
    return _fernet() is not None


def save_gemini_key(user_id: str, api_key: str) -> None:
    """Encrypt and persist a pasted Gemini key. Raises without the secret set."""
    f = _fernet()
    if f is None:
        raise RuntimeError(
            f"{ENCRYPTION_KEY_ENV} is not set — refusing to store a key in "
            "plaintext. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    from apps.api.jobs.store import get_store

    get_store().set_user_key(user_id, GEMINI_PROVIDER, f.encrypt(api_key.encode()).decode())


def load_gemini_key(user_id: str) -> Optional[str]:
    """Decrypt the stored key, or None (not saved / secret unset / rotated)."""
    from apps.api.jobs.store import get_store

    ciphertext = get_store().get_user_key(user_id, GEMINI_PROVIDER)
    if not ciphertext:
        return None
    f = _fernet()
    if f is None:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Encryption secret was rotated — the old ciphertext is unrecoverable.
        logger.warning("stored Gemini key for %s undecryptable (secret rotated?)", user_id)
        return None


def delete_gemini_key(user_id: str) -> bool:
    from apps.api.jobs.store import get_store

    return get_store().delete_user_key(user_id, GEMINI_PROVIDER)


def verify_gemini(
    api_key: Optional[str] = None,
    oauth_token: Optional[str] = None,
    timeout_s: float = 8.0,
) -> Tuple[bool, str]:
    """Probe with a credential against the API a run would actually use.

    Manual key → Gemini Developer API models list. OAuth token → a free
    Vertex AI ``countTokens`` call (the backend the graph selects for
    ``credentials``), which validates the token's scope, the project IAM, and
    that Vertex AI is enabled — so misconfiguration is surfaced in the UI
    instead of failing a run deep in the pipeline. Returns ``(ok, message)``.
    """
    headers = {"User-Agent": "drishti-api/1.0"}
    data = None
    if api_key:
        headers["x-goog-api-key"] = api_key
        url = f"{GEMINI_MODELS_URL}?pageSize=1"
    elif oauth_token:
        project = os.environ.get(PROJECT_ENV, "").strip()
        if not project:
            return False, (
                f"{PROJECT_ENV} is not set on the server — the Google-account "
                "path needs it; paste an API key instead"
            )
        headers["Authorization"] = f"Bearer {oauth_token}"
        headers["Content-Type"] = "application/json"
        url = VERTEX_COUNT_TOKENS_URL.format(project=project, model=PROBE_MODEL)
        data = json.dumps(
            {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
        ).encode()
    else:
        return False, "no credential to verify"

    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as res:
            if 200 <= res.status < 300:
                return True, "ok"
            return False, f"Gemini API answered HTTP {res.status}"
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode()).get("error", {}).get("message", "")
        except Exception:
            message = ""
        detail = f": {message}" if message else ""
        return False, f"Gemini API rejected the credential (HTTP {exc.code}){detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"could not reach the Gemini API: {exc}"


def resolve_gemini_provider_kwargs(user_id: str) -> dict:
    """Provider kwargs carrying the user's Gemini credential, for graph build.

    Manual key → ``{"api_key": ...}`` (mapped to ``google_api_key`` by the
    upstream Google client). OAuth → ``{"credentials": Credentials}`` (the
    forwarded kwarg added in FORK_PATCHES.md entry 7). Raises RuntimeError
    when the user has neither.
    """
    manual = load_gemini_key(user_id)
    if manual:
        return {"api_key": manual}

    from apps.api.clerk_users import get_google_oauth_token

    token = get_google_oauth_token(user_id) if OAUTH_ENABLED else None
    project = os.environ.get(PROJECT_ENV, "").strip()
    if token and project:
        from google.oauth2.credentials import Credentials  # dep of langchain-google-genai

        # A credentials object flips langchain-google-genai onto Vertex AI;
        # the google-genai client picks the project up from GOOGLE_CLOUD_PROJECT.
        # ponytail: the token is fetched once per run and never refreshed — a
        # run outliving the ~1h token lifetime will fail its later LLM calls.
        # Wire google.auth refresh via Clerk if very deep runs need it.
        return {"credentials": Credentials(token=token, quota_project_id=project)}

    raise RuntimeError(
        "Gemini runs use your own API key — paste one in the model settings"
    )


def gemini_credential_available(user_id: str) -> bool:
    """Submit-time check: does this user have any usable Gemini credential?"""
    if load_gemini_key(user_id):
        return True
    if not OAUTH_ENABLED:
        return False
    if not os.environ.get(PROJECT_ENV, "").strip():
        return False  # OAuth path needs the Vertex project — see module doc
    from apps.api.clerk_users import get_google_oauth_token

    return get_google_oauth_token(user_id) is not None
