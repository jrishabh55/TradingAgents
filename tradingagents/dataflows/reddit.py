"""Reddit search fetcher for ticker-specific discussion posts.

Two paths, picked by configuration:

- **App-only OAuth** (preferred) — set ``REDDIT_CLIENT_ID`` and
  ``REDDIT_CLIENT_SECRET`` (create a "script" app at reddit.com/prefs/apps).
  Uses the official ``oauth.reddit.com`` JSON search endpoint, which carries
  score / comment counts and — crucially — a per-client rate limit instead of
  the shared per-IP one, so it keeps working from datacenter IPs where the
  public endpoints 429/403.
- **Public Atom/RSS feed** (default, no credentials) —
  ``reddit.com/r/{sub}/search.rss``. The unauthenticated JSON endpoint is
  reliably WAF-blocked (``HTTP 403``, issue #862), so RSS is the whole
  unauthenticated story. On a 429 we back off once (honouring
  ``Retry-After``). RSS lacks score / comment counts, so those posts are
  marked and the formatter omits the metrics rather than printing fake zeros.

Returns formatted plaintext blocks ready for prompt injection and degrades
gracefully — OAuth failures fall back to RSS, and a fully empty fetch returns
a placeholder string rather than raising, so callers never special-case
missing data.
"""

from __future__ import annotations

import base64
import html
import http.client
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .fetch_proxy import urlopen_maybe_proxied
from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_API = "https://oauth.reddit.com/r/{sub}/search?{qs}"
# A descriptive, identified User-Agent (per Reddit's API etiquette). Reddit
# blocks generic/anonymous tokens like bare "Mozilla/5.0" or "curl/…" but
# serves this one on both endpoints; the RSS feed accepts it even when the
# JSON search endpoint 403s, so no browser-spoofing is needed.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Default subreddits ordered roughly by signal density for ticker-specific
# discussion. wallstreetbets has the most volume but most noise; stocks /
# investing trend more measured. Caller can override.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")


def _search_qs(ticker: str, limit: int) -> str:
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # last 7 days
        "limit": limit,
    })


def _iso_to_timestamp(iso_str: str | None) -> float | None:
    """Parse an Atom ``published`` timestamp to a UTC epoch, or None."""
    if not iso_str:
        return None
    try:
        normalized = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _strip_html(content: str) -> str:
    """Reduce the HTML body Reddit embeds in an Atom entry to plain text."""
    if not content:
        return ""
    # Reddit wraps the real selftext between SC_OFF / SC_ON markers.
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """Seconds to wait from a 429's ``Retry-After`` header, capped at 30s."""
    try:
        val = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return min(float(val), 30.0) if val else None
    except (ValueError, TypeError, AttributeError):
        return None


# App-only OAuth token cache. Tokens are per-app (not per-user) and live
# ~24h; the lock keeps concurrent analyses from racing a refresh.
_token_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0}


def _oauth_credentials() -> tuple[str, str] | None:
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def _drop_oauth_token() -> None:
    """Forget the cached token (401 handling; also used by tests)."""
    with _token_lock:
        _token["value"] = None
        _token["expires_at"] = 0.0


def _oauth_token(timeout: float) -> str | None:
    """Cached app-only bearer token, or None (no creds / auth failure)."""
    creds = _oauth_credentials()
    if creds is None:
        return None
    with _token_lock:
        if _token["value"] and time.time() < _token["expires_at"]:
            return _token["value"]
        basic = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        req = Request(
            _TOKEN_URL,
            data=urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"User-Agent": _UA, "Authorization": f"Basic {basic}"},
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            logger.warning("Reddit OAuth token fetch failed: %s", exc)
            return None
        token = payload.get("access_token")
        if not token:
            logger.warning("Reddit OAuth token response carried no access_token")
            return None
        # 60s slack so a token can't expire between check and use.
        _token["value"] = token
        _token["expires_at"] = time.time() + float(payload.get("expires_in", 3600)) - 60
        return token


def _fetch_subreddit_oauth(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    token: str,
) -> list[dict] | None:
    """Official JSON search via ``oauth.reddit.com`` — score/comments included.

    Returns None (caller falls back to RSS) on any failure; an empty list is a
    genuine "no posts" answer and is returned as-is. A 401 drops the cached
    token so the next call re-authenticates instead of failing for the rest of
    the token's assumed lifetime.
    """
    url = _OAUTH_API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={
        "User-Agent": _UA,
        "Authorization": f"bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 401:
            _drop_oauth_token()  # revoked/expired early — re-auth next call
        logger.warning(
            "Reddit OAuth search failed for r/%s · %s: %s — falling back to RSS.",
            sub, ticker, exc,
        )
        return None
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit OAuth search failed for r/%s · %s: %s — falling back to RSS.",
            sub, ticker, exc,
        )
        return None
    children = (payload.get("data") or {}).get("children") or []
    return [c.get("data", {}) for c in children if isinstance(c, dict)]


def _fetch_subreddit_rss(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    _retry: bool = True,
) -> list[dict]:
    """Default path: parse the public Atom search feed for a subreddit.

    Carries no score / comment counts, so those fields are left None and the
    post is tagged ``source="rss"`` for honest display. On a 429 (Reddit's
    per-IP rate limit) we back off once — honouring ``Retry-After`` when
    present — before giving up, so a transient burst doesn't blank the feed.
    """
    url = _RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA})
    try:
        # Rides the residential fetch proxy when the hosting layer provides
        # one (fetch_proxy.py) — Reddit's per-IP limit hits datacenter IPs
        # hard. Passing our urlopen keeps test patches on it effective.
        with urlopen_maybe_proxied(req, timeout=timeout, direct=urlopen) as resp:
            root = ET.fromstring(resp.read())
    except HTTPError as exc:
        if exc.code == 429 and _retry:
            wait = _retry_after_seconds(exc) or 5.0
            logger.warning(
                "Reddit RSS 429 for r/%s · %s — backing off %.1fs then retrying once",
                sub, ticker, wait,
            )
            time.sleep(wait)
            return _fetch_subreddit_rss(ticker, sub, limit, timeout, _retry=False)
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []

    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        posts.append({
            "title": (title_el.text if title_el is not None else "") or "",
            "score": None,
            "num_comments": None,
            "created_utc": _iso_to_timestamp(
                published_el.text if published_el is not None else None
            ),
            "selftext": _strip_html(content_el.text if content_el is not None else ""),
            "source": "rss",
        })
    return posts


def _fetch_subreddit_json(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Richer JSON search path (carries score / comment counts).

    Reddit's WAF currently returns ``403 Blocked`` on this endpoint for
    non-OAuth clients (issue #862), so it is NOT used by default — calling it on
    every request only doubled our volume against the per-IP rate limit and
    triggered 429s on the RSS fallback. Kept for the day the WAF relaxes or an
    OAuth token is wired in; degrades to RSS on failure.
    """
    url = _API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen_maybe_proxied(req, timeout=timeout, direct=urlopen) as resp:
            payload = json.loads(resp.read())
        children = (payload.get("data") or {}).get("children") or []
        return [c.get("data", {}) for c in children if isinstance(c, dict)]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit JSON fetch failed for r/%s · %s: %s — falling back to RSS feed.",
            sub, ticker, exc,
        )
        return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Fetch one subreddit: OAuth JSON when credentials are configured
    (richer posts, per-client rate limit that survives datacenter IPs),
    public RSS otherwise — and as the fallback when OAuth fails."""
    token = _oauth_token(timeout)
    if token:
        posts = _fetch_subreddit_oauth(ticker, sub, limit, timeout, token)
        if posts is not None:
            return posts
    return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,
) -> str:
    """Fetch recent Reddit posts mentioning ``ticker`` across finance
    subreddits and return them as a formatted plaintext block.

    ``inter_request_delay`` paces the (now RSS-only) per-subreddit requests to
    stay under Reddit's public per-IP rate limit; combined with the RSS-first
    path it makes 429s rare even when several analyses run back-to-back.
    """
    # Crypto reaches us as a Yahoo pair (BTC-USD); search Reddit for the base
    # ("BTC") so the query actually matches discussion instead of near-nothing.
    ticker = crypto_base(ticker) or ticker
    blocks = []
    total_posts = 0
    for i, sub in enumerate(subreddits):
        if i > 0:
            time.sleep(inter_request_delay)
        posts = _fetch_subreddit(ticker, sub, limit_per_sub, timeout)
        total_posts += len(posts)
        if not posts:
            blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
            continue

        via_rss = any(p.get("source") == "rss" for p in posts)
        header = f"r/{sub} — {len(posts)} recent posts mentioning {ticker.upper()}"
        header += " (via RSS feed; scores/comments unavailable):" if via_rss else ":"
        lines = [header]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score")
            comments = p.get("num_comments")
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            # Score / comment counts are absent on the RSS fallback path —
            # show them only when present rather than printing fake zeros.
            meta = created_str
            if score is not None and comments is not None:
                meta += f" · {score:>4}↑ · {comments:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        return (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
        )
    return "\n\n".join(blocks)
