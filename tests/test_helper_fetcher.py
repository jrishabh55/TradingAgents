"""Helper-side residential fetch executor — apps/helper/fetcher.py.

The security posture lives here: the allowlist and header filtering protect
the USER'S machine from the server, so these tests are the contract.
"""
from __future__ import annotations

import base64
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from apps.helper import fetcher


class _Resp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]


class _Opener:
    def __init__(self, resp):
        self._resp = resp
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _fetch(body, resp):
    opener = _Opener(resp)
    with patch.object(fetcher, "build_opener", return_value=opener):
        return fetcher._fetch_blocking(body), opener


@pytest.mark.unit
class TestAllowlist:
    def test_allowlisted_https_get_passes(self):
        out, opener = _fetch(
            {"url": "https://www.reddit.com/r/stocks/search.rss?q=NVDA"},
            _Resp(200, b"<feed/>"),
        )
        assert out["ok"] is True and out["status"] == 200
        assert base64.b64decode(out["body_b64"]) == b"<feed/>"
        assert len(opener.requests) == 1

    @pytest.mark.parametrize("url,why", [
        ("http://www.reddit.com/x", "https"),                 # plaintext
        ("https://evil.example/x", "allowlist"),              # off-list host
        ("https://www.reddit.com.evil.example/x", "allowlist"),  # suffix trick
        ("https://192.168.1.1/admin", "allowlist"),           # LAN SSRF
        ("", "https"),                                        # garbage
    ])
    def test_rejected_urls_never_touch_the_network(self, url, why):
        out, opener = _fetch({"url": url}, _Resp(200, b""))
        assert out["ok"] is False and why in out["error"].lower().replace("'", "")
        assert opener.requests == []

    def test_non_get_is_rejected(self):
        out, opener = _fetch(
            {"url": "https://www.reddit.com/x", "method": "POST"}, _Resp(200, b"")
        )
        assert out["ok"] is False and "GET" in out["error"]
        assert opener.requests == []


@pytest.mark.unit
class TestHeaderAndSizeHygiene:
    def test_identity_headers_are_stripped(self):
        out, opener = _fetch(
            {
                "url": "https://api.stocktwits.com/api/2/streams/symbol/NVDA.json",
                "headers": {
                    "User-Agent": "tradingagents/0.2",
                    "Accept": "application/json",
                    "Authorization": "Bearer sneaky",
                    "Cookie": "session=abc",
                },
            },
            _Resp(200, b"{}"),
        )
        assert out["ok"] is True
        sent = {k.lower(): v for k, v in opener.requests[0].header_items()}
        assert "authorization" not in sent and "cookie" not in sent
        assert sent["user-agent"] == "tradingagents/0.2"

    def test_oversized_response_is_refused(self):
        big = b"x" * (fetcher.MAX_BODY_BYTES + 1)
        out, _ = _fetch({"url": "https://www.reddit.com/x"}, _Resp(200, big))
        assert out["ok"] is False and "size" in out["error"]


@pytest.mark.unit
class TestUpstreamAnswers:
    def test_http_error_is_a_completed_exchange_not_a_failure(self):
        """A 429 from Reddit is the answer the server-side caller needs to
        see (it drives the existing backoff), not a proxy failure."""
        exc = HTTPError("https://www.reddit.com/x", 429, "rate limited", None, None)
        exc.read = lambda n=-1: b"slow down"  # type: ignore[method-assign]
        out, _ = _fetch({"url": "https://www.reddit.com/x"}, exc)
        assert out == {"ok": True, "status": 429,
                       "body_b64": base64.b64encode(b"slow down").decode()}

    def test_network_failure_is_a_proxy_failure(self):
        out, _ = _fetch({"url": "https://www.reddit.com/x"}, OSError("conn reset"))
        assert out["ok"] is False and "conn reset" in out["error"]
