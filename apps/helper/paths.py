"""Cross-platform state paths and secret-file handling.

Windows correctness matters here, so the rules are:

* every path is built with ``pathlib`` — never string concatenation or ``/``
* ``Path.home()`` resolves ``%USERPROFILE%`` on Windows, so ``~/.tradingagents``
  works unchanged and stays consistent with the rest of this repo (the job store
  already lives at ``~/.tradingagents/webapp.sqlite``)
* ``chmod(0o600)`` is close to a no-op on Windows. We still call it, but
  :func:`permissions_enforced` reports the truth so status output does not claim
  a protection the platform did not apply. On Windows the user-profile directory
  is already ACL-restricted to the account, which is the practical equivalent.
* writes are atomic via a temp file in the SAME directory then ``os.replace``,
  which is atomic on both POSIX and Windows. A temp file elsewhere could land on
  another filesystem and turn the replace into a copy.
"""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

#: Env override so the API, the helper and tests can share one location without
#: touching a developer's real state.
STATE_DIR_ENV = "TRADINGAGENTS_HOME"

_TOKEN_BYTES = 32


def permissions_enforced() -> bool:
    """Whether POSIX mode bits actually restrict access on this platform."""
    return os.name != "nt"


def state_dir() -> Path:
    """The helper's state directory, created if missing."""
    override = os.environ.get(STATE_DIR_ENV)
    d = Path(override).expanduser() if override else Path.home() / ".tradingagents"
    d.mkdir(parents=True, exist_ok=True)
    if permissions_enforced():
        try:
            d.chmod(stat.S_IRWXU)  # 0700
        except OSError:
            # A pre-existing directory we do not own; the file mode below is
            # still applied, so fail soft rather than refuse to start.
            pass
    return d


def local_token_file() -> Path:
    """Where the helper's loopback bearer token lives."""
    return state_dir() / "helper_token"


def codex_auth_file() -> Path:
    """Codex CLI's credential file. Read-only as far as we are concerned.

    ``~/.codex`` is correct on Windows too — Codex uses ``%USERPROFILE%\\.codex``.
    """
    return Path.home() / ".codex" / "auth.json"


def write_secret(path: Path, data: str) -> None:
    """Atomically write ``data`` to ``path`` with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        if permissions_enforced():
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, path)  # atomic on POSIX and Windows
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def read_secret(path: Path) -> str | None:
    """Read a secret file, or None when it does not exist."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def ensure_local_token() -> str:
    """Return the loopback bearer token, generating it on first run.

    Binding to 127.0.0.1 is not authorization — any local process, and any web
    page able to fetch localhost, could otherwise spend the user's subscription.
    """
    path = local_token_file()
    existing = read_secret(path)
    if existing:
        return existing
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    write_secret(path, token + "\n")
    return token
