"""Opportunistically reuse a local CLI's existing login.

The point of this module is that reusing a CLI's credentials is a *general*
mechanism, not a Codex special case. Codex CLI, Gemini CLI and Claude Code all
park an OAuth token in a JSON file under the user's home directory; the only
things that differ are where the token sits, how expiry is expressed, and which
header the upstream wants.

So a new CLI is a spec — data — not a new class. That is justified by real
divergence measured on this machine, not by speculation:

* Codex ``~/.codex/auth.json``: ``tokens.access_token`` is a JWT; expiry is its
  ``exp`` claim, in **seconds**.
* Gemini ``~/.gemini/oauth_creds.json``: ``access_token`` is an opaque Google
  token with no claims at all; expiry is the ``expiry_date`` field, in
  **milliseconds**.

Two files, two expiry mechanisms, one algorithm.

**Availability must never be load-bearing.** A missing or expired CLI login is
not an error — it is simply "this source has nothing to offer", so the provider's
chain moves to the next one. That is what keeps the helper working identically
whether or not any particular CLI happens to be installed.

**Never written to.** Refresh grants rotate (measured for Codex in M0), so
refreshing from here would invalidate the CLI's own copy and break a tool the
user depends on.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from apps.helper.credentials import Credential, CredentialError

#: Refuse a token this close to expiry so a long run cannot die mid-flight.
EXPIRY_MARGIN_S = 300

#: ChatGPT metadata namespace on the Codex id_token.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


def _dig(data: Any, path: Sequence[str]) -> Any:
    """Walk a key path, returning None if any hop is missing."""
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _jwt_claims(token: Optional[str]) -> dict[str, Any]:
    """Decode a JWT payload without verifying — it is our own token.

    Returns {} for opaque tokens (Google's are not JWTs), which is why expiry
    cannot be derived from the token universally.
    """
    if not token or token.count(".") != 2:
        return {}
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001 — a malformed token just has no metadata
        return {}


@dataclass(frozen=True)
class CliTokenFileSpec:
    """Where and how one CLI stores its OAuth token."""

    name: str
    #: Path relative to the user's home, as parts — kept as parts so it is
    #: built with pathlib and works on Windows (%USERPROFILE%).
    home_parts: tuple[str, ...]
    #: Key path to the bearer token inside the JSON.
    token_path: tuple[str, ...]
    #: "jwt_exp" reads the exp claim from the token itself (seconds);
    #: "epoch_ms" / "epoch_s" read an explicit field at ``expiry_path``.
    expiry_mode: str = "jwt_exp"
    expiry_path: tuple[str, ...] = ()
    #: Key path to an account identifier, if the upstream needs one.
    account_path: tuple[str, ...] = ()
    #: Header the account id must be sent as, if any.
    account_header: Optional[str] = None
    #: Key path to an id_token whose claims carry plan metadata.
    id_token_path: tuple[str, ...] = ()
    #: What to tell the user when this source cannot help.
    remedy: str = ""
    #: Extra static headers this CLI's upstream expects.
    headers: dict[str, str] = field(default_factory=dict)

    def path(self) -> Path:
        return Path.home().joinpath(*self.home_parts)


CODEX_SPEC = CliTokenFileSpec(
    name="codex-cli",
    home_parts=(".codex", "auth.json"),
    token_path=("tokens", "access_token"),
    expiry_mode="jwt_exp",
    account_path=("tokens", "account_id"),
    account_header="chatgpt-account-id",
    id_token_path=("tokens", "id_token"),
    remedy=(
        "Run `codex login` to renew it. The helper will not refresh it itself, "
        "because the refresh grant rotates the token and would invalidate your "
        "Codex CLI session."
    ),
)

GEMINI_SPEC = CliTokenFileSpec(
    name="gemini-cli",
    home_parts=(".gemini", "oauth_creds.json"),
    token_path=("access_token",),
    # Google access tokens are opaque, so expiry cannot come from the token.
    expiry_mode="epoch_ms",
    expiry_path=("expiry_date",),
    remedy="Run `gemini` and sign in again to renew the credential.",
)


class CliTokenFileSource:
    """Reads a bearer from a local CLI's credential file. Read-only."""

    def __init__(self, spec: CliTokenFileSpec, path: Optional[Path] = None) -> None:
        self.spec = spec
        self.name = spec.name
        self._path = path or spec.path()

    # ---- availability is advisory, never load-bearing ----

    def available(self) -> bool:
        """True when this source might work. Cheap; does not validate deeply.

        A False here means "skip me", not "fail the run".
        """
        return self._path.exists()

    def _expires_at(self, data: dict[str, Any], token: str) -> Optional[int]:
        if self.spec.expiry_mode == "jwt_exp":
            exp = _jwt_claims(token).get("exp")
            return int(exp) if isinstance(exp, (int, float)) else None
        raw = _dig(data, self.spec.expiry_path)
        if not isinstance(raw, (int, float)):
            return None
        seconds = raw / 1000 if self.spec.expiry_mode == "epoch_ms" else raw
        return int(seconds)

    async def get(self) -> Credential:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CredentialError(
                f"no {self.spec.name} credentials at {self._path}",
                remedy=self.spec.remedy,
            ) from None
        except OSError as exc:
            raise CredentialError(f"cannot read {self._path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialError(
                f"{self._path} is not valid JSON", remedy=self.spec.remedy
            ) from exc

        token = _dig(data, self.spec.token_path)
        if not isinstance(token, str) or not token:
            raise CredentialError(
                f"{self._path} has no usable token at "
                f"{'.'.join(self.spec.token_path)}",
                remedy=self.spec.remedy,
            )

        expires_at = self._expires_at(data, token)
        if expires_at is not None and expires_at - time.time() < EXPIRY_MARGIN_S:
            # Falls through to the next source in the chain — this is the normal
            # path when a CLI login has simply gone stale, not a failure.
            raise CredentialError(
                f"the {self.spec.name} token has expired",
                remedy=self.spec.remedy,
            )

        headers = dict(self.spec.headers)
        account = _dig(data, self.spec.account_path) if self.spec.account_path else None
        plan = None
        if self.spec.id_token_path:
            claims = _jwt_claims(_dig(data, self.spec.id_token_path))
            meta = claims.get(_OPENAI_AUTH_CLAIM) or {}
            account = account or meta.get("chatgpt_account_id")
            plan = meta.get("chatgpt_plan_type")
        if account and self.spec.account_header:
            headers[self.spec.account_header] = str(account)

        return Credential(
            token=token,
            headers=headers,
            account_label=str(account) if account else self.spec.name,
            plan=plan,
            expires_at=expires_at,
        )


def codex_cli_source(path: Optional[Path] = None) -> CliTokenFileSource:
    return CliTokenFileSource(CODEX_SPEC, path)


def gemini_cli_source(path: Optional[Path] = None) -> CliTokenFileSource:
    return CliTokenFileSource(GEMINI_SPEC, path)
