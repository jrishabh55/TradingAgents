"""ta-helper CLI: ``serve``, ``login``, ``status``, ``logout``.

    python -m apps.helper serve
    python -m apps.helper login
    python -m apps.helper status
"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from typing import Optional

from apps.helper import paths
from apps.helper.credentials import CredentialError
from apps.helper.credentials.oauth import (
    DEFAULT_CALLBACK_PORT,
    CALLBACK_PATH,
    OwnOAuthSource,
    TokenStore,
    authorize_url,
    callback_port_available,
    exchange_code,
    pkce_pair,
    redirect_uri,
)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    token = paths.ensure_local_token()
    print(f"helper token: {paths.local_token_file()}")
    if not paths.permissions_enforced():
        print("  note: this platform does not enforce POSIX file modes; the file "
              "relies on your user-profile ACL instead.")
    print(f"listening on http://127.0.0.1:{args.port}/v1/{{provider}}/chat/completions")
    uvicorn.run("apps.helper.server:app", factory=True, host="127.0.0.1", port=args.port)
    return 0


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler capturing ?code= from the OAuth redirect."""

    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}
        body = b"<html><body><h3>Signed in. You can close this tab.</h3></body></html>"
        if "error" in type(self).result:
            body = b"<html><body><h3>Sign-in failed. Check the terminal.</h3></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request logging
        return


def _login(args: argparse.Namespace) -> int:
    port = args.port
    if not callback_port_available(port):
        # U1 is unresolved: we do not know whether OpenAI accepts a redirect URI
        # on any port other than 1455, so we cannot silently pick another.
        print(
            f"error: port {port} is already in use — most likely a `codex login` "
            f"is running.\n"
            f"       Close it and retry. If you want to try another port, pass "
            f"--port; be aware OpenAI may reject a redirect_uri it does not "
            f"recognise.",
            file=sys.stderr,
        )
        return 2

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(port=port, challenge=challenge, state=state)

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"redirect: {redirect_uri(port)}")
    print("opening your browser to sign in…")
    print(f"if it does not open, visit:\n  {url}\n")
    webbrowser.open(url)

    thread.join(timeout=args.timeout)
    server.server_close()
    result = _CallbackHandler.result
    if not result:
        print(f"error: no callback received within {args.timeout}s", file=sys.stderr)
        return 3
    if "error" in result:
        print(f"error: {result.get('error')}: {result.get('error_description','')}",
              file=sys.stderr)
        return 4
    if result.get("state") != state:
        # Mismatched state means the response did not come from the request we
        # started; treat it as hostile rather than merely odd.
        print("error: state mismatch — aborting", file=sys.stderr)
        return 5

    try:
        tokens = asyncio.run(exchange_code(result["code"], verifier, port=port))
    except CredentialError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 6

    store = TokenStore()
    store.save(tokens)
    print(f"signed in; credential stored at {store.path}")
    return 0


def _status(_args: argparse.Namespace) -> int:
    from apps.helper.credentials.cli_file import codex_cli_source, gemini_cli_source

    print(f"state dir: {paths.state_dir()}")
    print(f"posix permissions enforced: {paths.permissions_enforced()}")
    sources = [codex_cli_source(), gemini_cli_source(), OwnOAuthSource()]
    for src in sources:
        present = getattr(src, "available", lambda: True)()
        if not present:
            print(f"  {src.name:14s} not present")
            continue
        try:
            cred = asyncio.run(src.get())
            plan = f", plan {cred.plan}" if cred.plan else ""
            print(f"  {src.name:14s} USABLE (account {cred.account_label}{plan})")
        except CredentialError as exc:
            print(f"  {src.name:14s} declines: {exc.message}")
            if exc.remedy:
                print(f"  {'':14s}   -> {exc.remedy}")
    return 0


def _logout(_args: argparse.Namespace) -> int:
    store = TokenStore()
    store.clear()
    print(f"removed {store.path} (a CLI's own login is untouched)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the loopback API")
    p.add_argument("--port", type=int, default=8899)
    p.set_defaults(func=_serve)

    p = sub.add_parser("login", help="sign in with ChatGPT (no Codex CLI needed)")
    p.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT)
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=_login)

    p = sub.add_parser("status", help="show which credential sources are usable")
    p.set_defaults(func=_status)

    p = sub.add_parser("logout", help="remove this helper's own credential")
    p.set_defaults(func=_logout)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
