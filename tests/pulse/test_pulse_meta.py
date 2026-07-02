"""Meta comments adapter: fixture parsing and API error handling."""

from __future__ import annotations

import httpx
import pytest

from helpers_pulse import PULSE_FIXTURE_DIR
from pulse.config import Settings
from pulse.sources.base import CredentialsError, SourceError
from pulse.sources.meta_comments import MetaCommentsAdapter, _normalize_time


@pytest.fixture
def adapter(pulse_settings) -> MetaCommentsAdapter:
    return MetaCommentsAdapter(pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)


def test_fixture_collect_filters_own_replies(adapter):
    items = adapter.collect(since=None)
    ids = {i.external_id for i in items}
    assert "875301_104" not in ids  # comment by the page itself
    assert len(items) == 7


def test_comment_normalization(adapter):
    by_id = {i.external_id: i for i in adapter.collect(since=None)}
    comment = by_id["881442_202"]
    assert "bloedverdunners" in comment.text
    assert comment.author_display == "Wilma van Leeuwen"
    assert comment.author_ref == "9210000000000202"
    assert comment.happened_at == "2026-06-30T19:18:00+00:00"
    assert comment.raw["post_id"].endswith("881442")
    assert "9210000000000202" not in comment.raw_json()  # no author id in raw


def test_normalize_time_formats():
    assert _normalize_time("2026-06-28T19:04:11+0000") == "2026-06-28T19:04:11+00:00"
    assert _normalize_time(None) is None


def _api_adapter(handler) -> MetaCommentsAdapter:
    pulse_settings = Settings(meta_page_id="123", meta_page_token="tok")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaCommentsAdapter(pulse_settings, client=client)


def test_expired_token_raises_credentials_error():
    def handler(request):
        return httpx.Response(
            400,
            json={"error": {"code": 190, "message": "Token expired"}},
            headers={"content-type": "application/json"},
        )

    adapter = _api_adapter(handler)
    with pytest.raises(CredentialsError):
        adapter.collect(since=None)


def test_api_error_raises_source_error():
    def handler(request):
        return httpx.Response(
            500,
            json={"error": {"code": 1, "message": "kapot"}},
            headers={"content-type": "application/json"},
        )

    adapter = _api_adapter(handler)
    with pytest.raises(SourceError):
        adapter.collect(since=None)


def test_unconfigured_collect_raises_credentials_error(pulse_settings):
    with pytest.raises(CredentialsError):
        MetaCommentsAdapter(pulse_settings).collect(since=None)


def test_verify_reports_page_name():
    def handler(request):
        return httpx.Response(
            200,
            json={"id": "123", "name": "Cloudplunge"},
            headers={"content-type": "application/json"},
        )

    result = _api_adapter(handler).verify()
    assert result.ok and "Cloudplunge" in result.message
