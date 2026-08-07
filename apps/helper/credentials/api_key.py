"""Tier C — a plain API key.

Cheap to support because the helper already speaks Chat Completions inbound, and
it keeps the architecture honest: the helper's job is "give the pipeline an LLM
endpoint", with the credential source pluggable. A user who would rather pay per
token, or point at a local Ollama, uses this and never touches OAuth.
"""
from __future__ import annotations

import os
from typing import Optional

from apps.helper.credentials import Credential, CredentialError


class ApiKeySource:
    """Reads a key from an explicit value or an environment variable."""

    name = "api-key"

    def __init__(self, *, key: Optional[str] = None, env_var: str = "OPENAI_API_KEY",
                 allow_missing: bool = False, placeholder: str = "EMPTY") -> None:
        self._key = key
        self._env_var = env_var
        # Local runtimes (Ollama, vLLM) authenticate with nothing; sending a
        # placeholder is what the upstream client library expects.
        self._allow_missing = allow_missing
        self._placeholder = placeholder

    def available(self) -> bool:
        return bool(self._key or os.environ.get(self._env_var) or self._allow_missing)

    async def get(self) -> Credential:
        key = self._key or os.environ.get(self._env_var)
        if not key:
            if self._allow_missing:
                return Credential(token=self._placeholder, account_label="keyless")
            raise CredentialError(
                f"no API key available (checked {self._env_var})",
                remedy=f"Set {self._env_var}, or use a subscription credential source.",
            )
        # Fingerprint rather than the key itself: `principal()` feeds the
        # idempotency cache key, which must not contain a secret.
        return Credential(token=key, account_label=f"{self._env_var}:{key[-4:]}")
