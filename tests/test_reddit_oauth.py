"""App-only OAuth path for the Reddit fetcher — tradingagents/dataflows/reddit.py.

With REDDIT_CLIENT_ID/SECRET set, fetches go through oauth.reddit.com (richer
posts, per-client rate limit that works from datacenter IPs). Without them —
or on any OAuth failure — behavior is exactly the RSS path the fallback tests
already cover.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import reddit
from tests.test_reddit_fallback import _resp


def _json_resp(obj):
    return _resp(lambda: json.dumps(obj).encode())


_TOKEN_PAYLOAD = {"access_token": "tok-123", "expires_in": 86400, "token_type": "bearer"}

_SEARCH_PAYLOAD = {
    "data": {
        "children": [
            {"kind": "t3", "data": {
                "title": "NVDA earnings beat",
                "score": 512,
                "num_comments": 87,
                "created_utc": 1747751400.0,
                "selftext": "Great quarter.",
            }},
        ]
    }
}


@pytest.fixture(autouse=True)
def _fresh_token_cache(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    reddit._drop_oauth_token()
    yield
    reddit._drop_oauth_token()


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")


@pytest.mark.unit
class TestOauthDisabled:
    def test_no_creds_means_no_token_and_rss_path(self):
        assert reddit._oauth_token(5.0) is None
        with patch.object(reddit, "_fetch_subreddit_rss", return_value=[{"title": "x"}]) as rss:
            posts = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()
        assert posts == [{"title": "x"}]


@pytest.mark.unit
class TestOauthPath:
    def test_search_uses_bearer_and_returns_rich_posts(self, creds):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if "access_token" in req.full_url:
                assert req.headers.get("Authorization", "").startswith("Basic ")
                return _json_resp(_TOKEN_PAYLOAD)
            assert req.full_url.startswith("https://oauth.reddit.com/")
            assert req.headers.get("Authorization") == "bearer tok-123"
            return _json_resp(_SEARCH_PAYLOAD)

        with patch.object(reddit, "urlopen", fake_urlopen):
            posts = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        assert posts[0]["score"] == 512 and posts[0]["num_comments"] == 87

    def test_token_is_cached_across_fetches(self, creds):
        token_fetches = []

        def fake_urlopen(req, timeout=None):
            if "access_token" in req.full_url:
                token_fetches.append(1)
                return _json_resp(_TOKEN_PAYLOAD)
            return _json_resp(_SEARCH_PAYLOAD)

        with patch.object(reddit, "urlopen", fake_urlopen):
            reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
            reddit._fetch_subreddit("AAPL", "investing", 5, 5.0)
        assert len(token_fetches) == 1

    def test_empty_result_is_genuine_not_a_fallback(self, creds):
        def fake_urlopen(req, timeout=None):
            if "access_token" in req.full_url:
                return _json_resp(_TOKEN_PAYLOAD)
            return _json_resp({"data": {"children": []}})

        with patch.object(reddit, "urlopen", fake_urlopen), \
             patch.object(reddit, "_fetch_subreddit_rss") as rss:
            posts = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_not_called()
        assert posts == []


@pytest.mark.unit
class TestOauthDegradation:
    def test_search_failure_falls_back_to_rss(self, creds):
        def fake_urlopen(req, timeout=None):
            if "access_token" in req.full_url:
                return _json_resp(_TOKEN_PAYLOAD)
            raise HTTPError(req.full_url, 500, "boom", None, None)

        with patch.object(reddit, "urlopen", fake_urlopen), \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=[]) as rss:
            reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()

    def test_401_drops_the_cached_token(self, creds):
        def fake_urlopen(req, timeout=None):
            if "access_token" in req.full_url:
                return _json_resp(_TOKEN_PAYLOAD)
            raise HTTPError(req.full_url, 401, "expired", None, None)

        with patch.object(reddit, "urlopen", fake_urlopen), \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=[]):
            reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        assert reddit._token["value"] is None

    def test_token_endpoint_failure_degrades_to_rss(self, creds):
        def fake_urlopen(req, timeout=None):
            raise HTTPError(req.full_url, 403, "blocked", None, None)

        with patch.object(reddit, "urlopen", fake_urlopen), \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=[]) as rss:
            reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()
