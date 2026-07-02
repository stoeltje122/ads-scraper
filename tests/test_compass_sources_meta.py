"""MetaInsightsSource: exact-cents parsing of the committed fixture rows,
the purchase-priority rules and the HTTP behaviour (pagination, retries,
throttling), all offline via httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from compass.config import Settings
from compass.sources.base import CredentialsError, SourceError
from compass.sources.meta_insights import (
    BACKOFF_BASE_SECONDS,
    INSIGHTS_FIELDS,
    MetaInsightsSource,
    check_token,
    parse_insight_row,
)

TOKEN = "meta-test-token"
ACCOUNT_ID = "1234567890"
PROSPECTING = "23850000000000101"
RETARGETING = "23850000000000102"
BRAND = "23850000000000103"
JUNE_1 = date(2026, 6, 1)
JUNE_30 = date(2026, 6, 30)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "compass" / "meta_insights.json"


def _settings(account_id: str = ACCOUNT_ID) -> Settings:
    return Settings(meta_access_token=TOKEN, meta_ad_account_id=account_id)


def _source(handler, account_id: str = ACCOUNT_ID) -> MetaInsightsSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaInsightsSource(_settings(account_id), client=client)


def fixture_rows() -> list[dict]:
    """Fresh copies of the committed fixture rows (safe to mutate per test)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]


def fixture_row(campaign_id: str, day: str) -> dict:
    return next(
        r for r in fixture_rows() if r["campaign_id"] == campaign_id and r["date_start"] == day
    )


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture time.sleep calls inside the meta_insights module (no real waiting)."""
    calls: list[float] = []
    monkeypatch.setattr("compass.sources.meta_insights.time.sleep", calls.append)
    return calls


# ── constructor / credentials ────────────────────────────────────────


def test_constructor_without_account_id_raises_credentials_error():
    with pytest.raises(CredentialsError) as excinfo:
        MetaInsightsSource(Settings(meta_access_token=TOKEN))
    assert "META_AD_ACCOUNT_ID" in str(excinfo.value)


def test_constructor_without_token_raises_credentials_error():
    with pytest.raises(CredentialsError):
        MetaInsightsSource(Settings(meta_ad_account_id=ACCOUNT_ID))


@pytest.mark.parametrize("raw", ["1234567890", "act_1234567890", "  1234567890  "])
def test_account_id_is_normalized_to_act_prefix(raw):
    source = _source(lambda request: httpx.Response(200, json={}), account_id=raw)
    assert source.account_id == "act_1234567890"


# ── parse_insight_row: exact cents and purchase priority ─────────────


def test_parse_prospecting_row_exact_cents_and_omni_priority():
    row = fixture_row(PROSPECTING, "2026-06-28")
    rec = parse_insight_row(row)

    assert rec.day == date(2026, 6, 28)
    assert rec.campaign_id == PROSPECTING
    assert rec.campaign_name == "Prospecting – Advantage+"
    assert rec.spend_cents == 24567
    assert rec.impressions == 18234
    assert rec.clicks == 312
    # omni_purchase (9 / € 269,55) beats the pixel purchase (8 / € 239,60)
    assert rec.meta_purchases == 9
    assert rec.meta_purchase_value_cents == 26955
    assert rec.raw is row


def test_parse_falls_back_to_purchase_when_omni_is_absent():
    rec = parse_insight_row(fixture_row(RETARGETING, "2026-06-28"))

    assert rec.spend_cents == 8820
    assert rec.meta_purchases == 4
    assert rec.meta_purchase_value_cents == 11980


def test_parse_falls_back_to_pixel_purchase_as_last_resort():
    rec = parse_insight_row(fixture_row(PROSPECTING, "2026-06-29"))

    assert rec.spend_cents == 25210
    assert rec.meta_purchases == 10
    assert rec.meta_purchase_value_cents == 29950


def test_parse_row_without_purchases_stays_none_not_zero():
    # Non-purchase action types (link_click etc.) must never count as purchases,
    # and missing attribution must stay None so 'geen pixel' remains visible.
    row = fixture_row(BRAND, "2026-06-28")
    assert any(a["action_type"] == "link_click" for a in row["actions"])

    rec = parse_insight_row(row)

    assert rec.spend_cents == 3105
    assert rec.meta_purchases is None
    assert rec.meta_purchase_value_cents is None


def test_parse_empty_action_values_list_stays_none():
    rec = parse_insight_row(fixture_row(BRAND, "2026-06-29"))

    assert rec.meta_purchases is None
    assert rec.meta_purchase_value_cents is None


def test_parse_missing_spend_and_counters_default_sanely():
    row = fixture_row(BRAND, "2026-06-28")
    row.pop("spend")
    row.pop("impressions")
    row.pop("clicks")

    rec = parse_insight_row(row)

    assert rec.spend_cents == 0  # spend is additive, so 0 is safe
    assert rec.impressions is None
    assert rec.clicks is None


# ── fetch_daily_spend: params and pagination ─────────────────────────


def test_fetch_daily_spend_sends_the_contracted_insights_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": fixture_rows()})

    records = list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))

    assert len(records) == 6
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v23.0/act_1234567890/insights"
    params = request.url.params
    assert params["level"] == "campaign"
    assert params["time_increment"] == "1"
    assert json.loads(params["time_range"]) == {"since": "2026-06-01", "until": "2026-06-30"}
    assert params["fields"] == INSIGHTS_FIELDS
    assert params["limit"] == "500"
    assert params["access_token"] == TOKEN


def test_pagination_follows_paging_next_across_two_pages():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("after") == "CURSOR2":
            return httpx.Response(200, json={"data": [fixture_row(RETARGETING, "2026-06-29")]})
        return httpx.Response(
            200,
            json={
                "data": [fixture_row(PROSPECTING, "2026-06-28")],
                "paging": {
                    "next": "https://graph.facebook.com/v23.0/act_1234567890/insights"
                    f"?access_token={TOKEN}&after=CURSOR2"
                },
            },
        )

    records = list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))

    assert [(r.campaign_id, r.day) for r in records] == [
        (PROSPECTING, date(2026, 6, 28)),
        (RETARGETING, date(2026, 6, 29)),
    ]
    assert len(seen) == 2
    assert seen[1].url.params["after"] == "CURSOR2"


# ── errors, retries and throttling ───────────────────────────────────


def test_error_190_raises_credentials_error_without_retry(sleeps):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            400, json={"error": {"code": 190, "message": "Error validating access token"}}
        )

    with pytest.raises(CredentialsError) as excinfo:
        list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))

    assert "META_ACCESS_TOKEN" in str(excinfo.value)
    assert len(seen) == 1  # a dead token is never retried
    assert sleeps == []


def test_500_is_retried_with_exponential_backoff_then_succeeds(sleeps):
    responses = [
        httpx.Response(500, json={"error": {"code": 1, "message": "An unknown error occurred"}}),
        httpx.Response(200, json={"data": [fixture_row(PROSPECTING, "2026-06-28")]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    records = list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))

    assert len(records) == 1
    assert responses == []  # both responses consumed
    assert sleeps == [BACKOFF_BASE_SECONDS]  # exponential backoff, first step


def test_rate_limit_code_4_is_retried_then_succeeds(sleeps):
    responses = [
        httpx.Response(400, json={"error": {"code": 4, "message": "rate limited"}}),
        httpx.Response(200, json={"data": []}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))
    assert responses == []
    assert sleeps == [BACKOFF_BASE_SECONDS]


def test_non_retryable_graph_error_raises_source_error_immediately(sleeps):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": {"code": 100, "message": "bad param"}})

    with pytest.raises(SourceError, match="code 100"):
        list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))
    assert len(seen) == 1
    assert sleeps == []


@pytest.mark.parametrize(("worst_usage", "expected"), [(85, [20]), (96, [60]), (79, [])])
def test_app_usage_header_pauses_only_near_the_quota(sleeps, worst_usage, expected):
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

    list(_source(handler).fetch_daily_spend(JUNE_1, JUNE_30))
    assert sleeps == expected


# ── check_token / verify ─────────────────────────────────────────────


def test_check_token_without_token_is_offline():
    # No transport, no network: the empty-token path must return before any call.
    ok, message = check_token(Settings())
    assert ok is False
    assert "META_ACCESS_TOKEN" in message


def test_check_token_reports_the_account_name(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/me")
        assert kwargs["params"]["access_token"] == TOKEN
        return httpx.Response(200, json={"name": "Cloudplunge", "id": "42"})

    monkeypatch.setattr("compass.sources.meta_insights.httpx.get", fake_get)
    ok, message = check_token(_settings())
    assert ok is True
    assert "Cloudplunge" in message


def _verify_handler(account: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v23.0/act_1234567890/insights":
            return httpx.Response(200, json={"data": fixture_rows()})
        if path == "/v23.0/act_1234567890":
            assert request.url.params["fields"] == "name,currency,account_status"
            return httpx.Response(200, json=account)
        raise AssertionError(f"unexpected path: {path}")

    return handler


def test_verify_live_reports_account_spend_and_attribution():
    handler = _verify_handler(
        {"name": "Cloudplunge Ads", "currency": "EUR", "account_status": 1}
    )

    report = _source(handler).verify()

    assert report.ok is True
    assert report.mode == "live"
    assert report.source == "meta"
    assert any("Cloudplunge Ads" in line for line in report.lines)
    assert any("EUR" in line for line in report.lines)
    # Total spend of all six fixture rows, Dutch-formatted.
    assert any("€ 736,97" in line for line in report.lines)
    assert any("attributie werkt" in line for line in report.lines)


def test_verify_warns_hard_when_the_currency_is_not_eur():
    handler = _verify_handler({"name": "US Account", "currency": "USD", "account_status": 1})

    report = _source(handler).verify()

    assert any("geen EUR" in line for line in report.lines)
    assert any("USD" in line for line in report.lines)


def test_verify_turns_api_failure_into_dutch_diagnosis_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 100, "message": "bad request"}})

    report = _source(handler).verify()

    assert report.ok is False
    assert report.mode == "live"
    assert any("Meta" in line for line in report.lines)
