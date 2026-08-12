"""Start-at-login registration, toggled from the local UI.

Per platform, "registered" means one file exists; enable writes it, disable
deletes it, and its presence IS the state — no extra bookkeeping to drift.

* macOS   — a LaunchAgent plist in ``~/Library/LaunchAgents`` (picked up at
  next login; no ``launchctl`` dance needed for that).
* Windows — a ``.cmd`` in the user's Startup folder.
* Linux   — an XDG autostart ``.desktop`` entry.

Autostart runs ``--no-browser`` (headless server + relay reconnect). Opening
the app later just attaches a window to that running instance — see
``__main__._app``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_LABEL = "com.drishti.helper"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _launch_cmd() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller build: the binary is the app
        return [sys.executable, "--no-browser"]
    return [sys.executable, "-m", "apps.helper", "app", "--no-browser"]


def _target() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
    if os.name == "nt":
        return (Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft"
                / "Windows" / "Start Menu" / "Programs" / "Startup"
                / "DrishtiHelper.cmd")
    return Path.home() / ".config" / "autostart" / "drishti-helper.desktop"


def enabled() -> bool:
    return _target().exists()


def enable() -> None:
    # Dev runs need the repo as cwd for `-m apps.helper`. A frozen app must
    # NOT get one: _repo_root() would resolve inside PyInstaller's temp
    # extraction dir, which is gone by next login.
    frozen = getattr(sys, "frozen", False)
    cmd = _launch_cmd()
    target = _target()
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib

        entry: dict = {"Label": _LABEL, "ProgramArguments": cmd, "RunAtLoad": True}
        if not frozen:
            entry["WorkingDirectory"] = str(_repo_root())
        target.write_bytes(plistlib.dumps(entry))
    elif os.name == "nt":
        quoted = " ".join(f'"{c}"' for c in cmd)
        # ponytail: a Startup .cmd flashes a console briefly; a registry Run
        # key + pythonw would hide it — switch if users complain.
        lines = [] if frozen else [f'cd /d "{_repo_root()}"']
        lines.append(f'start "" {quoted}')
        target.write_text("\n".join(lines) + "\n")
    else:
        exec_line = " ".join(cmd)
        body = "[Desktop Entry]\nType=Application\nName=Drishti Helper\n"
        if not frozen:
            body += f"Path={_repo_root()}\n"
        target.write_text(body + f"Exec={exec_line}\n")


def disable() -> None:
    try:
        _target().unlink()
    except FileNotFoundError:
        pass
