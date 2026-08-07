"""Tier A — reuse an existing Codex CLI login. STRICTLY READ-ONLY.

Why read-only is not merely cautious: M0 measured the refresh grant and it
**rotates the refresh token**. Refreshing from here would invalidate the copy in
``~/.codex/auth.json``, silently breaking the user's ``codex`` CLI until they run
``codex login`` again. So this source uses the existing access token until it
expires and then hands off, rather than refreshing.

That is a comfortable trade: the measured access-token lifetime is ~10 days.

Never opens the file for writing. There is a test asserting exactly that,
because a well-meaning "just refresh it" change here would break a tool the user
depends on.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional

from apps.helper.credentials import Credential, CredentialError
from apps.helper.paths import codex_auth_file

#: Claim namespace holding ChatGPT account/plan metadata on the id_token.
_AUTH_CLAIM = "https://api.openai.com/auth"

#: Refuse a token this close to expiry so a long run does not die mid-flight.
_EXPIRY_MARGIN_S = 300


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying — we only read our own token."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001 — a malformed token is just "no metadata"
        return {}


class CodexAuthFileSource:
    """Reads the access token Codex CLI already obtained."""

    name = "codex-cli"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or codex_auth_file()

    def available(self) -> bool:
        return self._path.exists()

    async def get(self) -> Credential:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CredentialError(
                f"no Codex credentials at {self._path}",
                remedy="Run `codex login`, or sign in through the helper's own OAuth.",
            ) from None
        except OSError as exc:
            raise CredentialError(f"cannot read {self._path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialError(
                f"{self._path} is not valid JSON",
                remedy="Run `codex login` to rewrite it.",
            ) from exc

        tokens = data.get("tokens") or {}
        access = tokens.get("access_token")
        if not access:
            raise CredentialError(
                f"{self._path} has no access_token",
                remedy="Run `codex login`.",
            )

        claims = _jwt_claims(access)
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and exp - time.time() < _EXPIRY_MARGIN_S:
            # Deliberately do NOT refresh: the grant rotates the refresh token
            # and would break the user's Codex CLI.
            raise CredentialError(
                "the Codex access token has expired",
                remedy=(
                    "Run `codex login` to renew it. The helper will not refresh it "
                    "itself, because the refresh grant rotates the token and would "
                    "invalidate your Codex CLI session."
                ),
            )

        id_claims = _jwt_claims(tokens.get("id_token") or "")
        auth_meta = id_claims.get(_AUTH_CLAIM) or {}
        account_id = tokens.get("account_id") or auth_meta.get("chatgpt_account_id")

        headers = {}
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)

        return Credential(
            token=access,
            headers=headers,
            account_label=str(account_id) if account_id else None,
            plan=auth_meta.get("chatgpt_plan_type"),
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
        )
