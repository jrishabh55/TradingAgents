"""Tests for helper credential sources and cross-platform path handling."""
import asyncio
import base64
import json
import os
import time

import pytest

from apps.helper import paths
from apps.helper.credentials import Credential, CredentialError
from apps.helper.credentials.api_key import ApiKeySource
from apps.helper.credentials.codex_file import CodexAuthFileSource


def run(coro):
    """Drive a coroutine without pulling in pytest-asyncio for test-only need."""
    return asyncio.run(coro)


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg':'none'})}.{seg(claims)}.sig"


def _auth_json(tmp_path, *, exp_offset=86400, account="acct-123", plan="pro"):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": _jwt({"exp": time.time() + exp_offset}),
            "refresh_token": "r" * 20,
            "id_token": _jwt({"https://api.openai.com/auth": {
                "chatgpt_account_id": account, "chatgpt_plan_type": plan}}),
            "account_id": account,
        },
    }))
    return p


# ---------- paths: Windows-safe ----------


def test_state_dir_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "st"))
    d = paths.state_dir()
    assert d == tmp_path / "st" and d.is_dir()


def test_secret_write_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path))
    target = tmp_path / "secret"
    paths.write_secret(target, "value")
    assert paths.read_secret(target) == "value"
    # No .secret.*.tmp debris left behind.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_secret_write_overwrites_cleanly(tmp_path):
    target = tmp_path / "s"
    paths.write_secret(target, "one")
    paths.write_secret(target, "two")
    assert paths.read_secret(target) == "two"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_secret_is_owner_only_on_posix(tmp_path):
    target = tmp_path / "s"
    paths.write_secret(target, "x")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_permissions_enforced_reports_the_platform_truth():
    assert paths.permissions_enforced() == (os.name != "nt")


def test_read_secret_missing_file_is_none(tmp_path):
    assert paths.read_secret(tmp_path / "nope") is None


def test_local_token_is_generated_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path))
    first = paths.ensure_local_token()
    assert len(first) > 20
    assert paths.ensure_local_token() == first


def test_paths_are_built_with_pathlib_not_string_joins(tmp_path, monkeypatch):
    """Guards the Windows requirement — no hardcoded separators."""
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path))
    assert paths.local_token_file().parent == tmp_path
    assert paths.codex_auth_file().name == "auth.json"
    assert paths.codex_auth_file().parent.name == ".codex"


# ---------- Tier A: Codex file, read-only ----------


def test_reads_an_existing_codex_login(tmp_path):
    src = CodexAuthFileSource(_auth_json(tmp_path))
    cred = run(src.get())
    assert cred.token
    assert cred.headers["chatgpt-account-id"] == "acct-123"
    assert cred.plan == "pro"
    assert cred.principal() == "acct-123"


def test_expired_token_refuses_and_does_not_refresh(tmp_path):
    """The refresh grant rotates (measured in M0), so refreshing here would
    invalidate the user's working `codex login`."""
    src = CodexAuthFileSource(_auth_json(tmp_path, exp_offset=-10))
    with pytest.raises(CredentialError) as e:
        run(src.get())
    assert "expired" in str(e.value)
    assert "codex login" in e.value.remedy
    assert "rotates" in e.value.remedy


def test_never_opens_the_codex_file_for_writing(tmp_path, monkeypatch):
    """A regression guard: a well-meaning "just refresh it" change here would
    break a tool the user depends on."""
    path = _auth_json(tmp_path)
    real_open = open
    opened_modes = []

    def spy(file, mode="r", *a, **kw):
        if str(file) == str(path):
            opened_modes.append(mode)
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    run(CodexAuthFileSource(path).get())
    assert all("w" not in m and "a" not in m and "+" not in m for m in opened_modes)


def test_missing_file_gives_an_actionable_remedy(tmp_path):
    src = CodexAuthFileSource(tmp_path / "absent.json")
    assert src.available() is False
    with pytest.raises(CredentialError) as e:
        run(src.get())
    assert "codex login" in e.value.remedy


def test_malformed_json_is_reported_clearly(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{not json")
    with pytest.raises(CredentialError, match="not valid JSON"):
        run(CodexAuthFileSource(p).get())


def test_missing_access_token_is_reported(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
    with pytest.raises(CredentialError, match="no access_token"):
        run(CodexAuthFileSource(p).get())


# ---------- Tier C: API key ----------


def test_api_key_from_explicit_value():
    cred = run(ApiKeySource(key="sk-abcd1234").get())
    assert cred.token == "sk-abcd1234"
    # principal() feeds the cache key, so it must not be the secret.
    assert "sk-abcd1234" not in cred.principal()


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-zzz9")
    src = ApiKeySource(env_var="MY_KEY")
    assert src.available()
    assert (run(src.get())).token == "sk-zzz9"


def test_keyless_local_runtime_gets_a_placeholder(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cred = run(ApiKeySource(allow_missing=True).get())
    assert cred.token == "EMPTY" and cred.account_label == "keyless"


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(CredentialError) as e:
        run(ApiKeySource().get())
    assert "OPENAI_API_KEY" in e.value.remedy


def test_credential_principal_never_returns_a_token():
    assert Credential(token="secret-value").principal() == "unknown"
