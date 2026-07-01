"""MetaAdLibraryAPI HTTP behaviour, tested offline via httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from adscout.config import Settings
from adscout.sources.base import AdSourceError, TokenError
from adscout.sources.meta import (
    BACKOFF_BASE_SECONDS,
    MetaAdLibraryAPI,
    check_token,
)

TOKEN = "test-token"


def _api(handler) -> MetaAdLibraryAPI:
    settings = Settings(meta_access_token=TOKEN)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaAdLibraryAPI(settings, client=client)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture time.sleep calls inside the meta module (no real waiting)."""
    calls: list[float] = []
    monkeypatch.setattr("adscout.sources.meta.time.sleep", calls.append)
    return calls


def test_constructor_without_token_raises_token_error():
    with pytest.raises(TokenError):
        MetaAdLibraryAPI(Settings(meta_access_token=""))


def test_pagination_follows_paging_next_across_two_pages():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("after") == "CURSOR2":
            return httpx.Response(200, json={"data": [{"id": "2", "page_id": "111"}]})
        return httpx.Response(
            200,
            json={
                "data": [{"id": "1", "page_id": "111"}],
                "paging": {
                    "next": "https://graph.facebook.com/v23.0/ads_archive"
                    f"?access_token={TOKEN}&after=CURSOR2"
                },
            },
        )

    records = list(_api(handler).fetch_ads(["111"], "NL"))

    assert [r.ad_archive_id for r in records] == ["1", "2"]
    assert len(seen) == 2
    first, second = seen
    assert first.url.path == "/v23.0/ads_archive"
    assert first.url.params["ad_reached_countries"] == '["NL"]'
    assert first.url.params["ad_active_status"] == "ALL"
    assert second.url.params["after"] == "CURSOR2"


def test_error_190_raises_token_error_without_retry():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            400, json={"error": {"code": 190, "message": "Error validating access token"}}
        )

    api = _api(handler)
    with pytest.raises(TokenError) as excinfo:
        list(api.fetch_ads(["111"], "NL"))

    assert "META_ACCESS_TOKEN" in str(excinfo.value)
    assert len(seen) == 1  # a dead token is never retried


def test_rate_limit_code_4_is_retried_then_succeeds(sleeps):
    responses = [
        httpx.Response(400, json={"error": {"code": 4, "message": "rate limited"}}),
        httpx.Response(200, json={"data": [{"id": "1", "page_id": "111"}]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    records = list(_api(handler).fetch_ads(["111"], "NL"))

    assert [r.ad_archive_id for r in records] == ["1"]
    assert responses == []  # both responses consumed
    assert sleeps == [BACKOFF_BASE_SECONDS]  # exponential backoff, first step


def test_non_retryable_graph_error_raises_immediately(sleeps):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": {"code": 100, "message": "bad param"}})

    with pytest.raises(AdSourceError, match="code 100"):
        list(_api(handler).fetch_ads(["111"], "NL"))
    assert len(seen) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("worst_usage", "expected_pause"),
    [(85, 20), (96, 60)],
)
def test_high_app_usage_header_triggers_pause(sleeps, worst_usage, expected_pause):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": []},
            headers={
                "x-app-usage": json.dumps(
                    {"call_count": worst_usage, "total_time": 1, "total_cputime": 1}
                )
            },
        )

    list(_api(handler).fetch_ads(["111"], "NL"))
    assert sleeps == [expected_pause]


def test_low_app_usage_header_does_not_pause(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": []},
            headers={"x-app-usage": json.dumps({"call_count": 79, "total_time": 0, "total_cputime": 0})},
        )

    list(_api(handler).fetch_ads(["111"], "NL"))
    assert sleeps == []


def test_search_page_ids_chunks_more_than_ten_pages_into_two_calls():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    page_ids = [str(1000 + i) for i in range(12)]
    list(_api(handler).fetch_ads(page_ids, "NL"))

    assert len(seen) == 2
    chunks = [json.loads(req.url.params["search_page_ids"]) for req in seen]
    assert chunks[0] == page_ids[:10]
    assert chunks[1] == page_ids[10:]


def test_check_token_without_token_is_offline():
    # No transport, no network: the empty-token path must return before any call.
    ok, message = check_token(Settings(meta_access_token=""))
    assert ok is False
    assert "geen META_ACCESS_TOKEN" in message
