"""ShopifySource: exact-cents parsing of the committed fixture orders and
the HTTP behaviour (pagination, rate limits, credentials), all offline via
httpx.MockTransport."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from compass.config import Settings
from compass.models import customer_hash
from compass.sources.base import CredentialsError, SourceError
from compass.sources.shopify import (
    BACKOFF_BASE_SECONDS,
    CALL_LIMIT_PAUSE_SECONDS,
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_RETRIES,
    ShopifySource,
    check_access,
    parse_order,
)

SHOP = "cloudplunge-test.myshopify.com"
TOKEN = "shpat_test"
JUNE_1 = date(2026, 6, 1)
JUNE_30 = date(2026, 6, 30)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "compass" / "shopify_orders.json"


def _settings() -> Settings:
    return Settings(shopify_shop=SHOP, shopify_access_token=TOKEN)


def _source(handler) -> ShopifySource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ShopifySource(_settings(), client=client)


def fixture_orders() -> list[dict]:
    """Fresh copies of the committed fixture orders (safe to mutate per test)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["orders"]


def fixture_order(order_id: int) -> dict:
    return next(o for o in fixture_orders() if o["id"] == order_id)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture time.sleep calls inside the shopify module (no real waiting)."""
    calls: list[float] = []
    monkeypatch.setattr("compass.sources.shopify.time.sleep", calls.append)
    return calls


# ── constructor / credentials ────────────────────────────────────────


def test_constructor_without_credentials_raises_credentials_error():
    with pytest.raises(CredentialsError) as excinfo:
        ShopifySource(Settings())
    assert "SHOPIFY" in str(excinfo.value)


# ── parse_order: exact cents per fixture order ───────────────────────


def test_parse_ideal_order_exact_cents_and_identity():
    item = fixture_order(6100000000001)
    rec = parse_order(item)

    assert rec is not None
    assert rec.extern_id == "6100000000001"
    assert rec.channel == "shopify"
    assert rec.gross_cents == 2995
    assert rec.vat_cents == 247
    assert rec.net_cents == 2748  # matches the metrics reference case
    assert rec.units == 1
    assert rec.payment_method == "ideal"  # lowercased from "iDEAL"
    assert rec.status == "paid"
    assert rec.refunded_cents == 0
    assert rec.is_new_customer is True
    assert rec.customer_hash == customer_hash("anna@example.com")
    assert rec.ordered_at == datetime.fromisoformat("2026-06-05T10:15:02+02:00")
    assert rec.order_day == date(2026, 6, 5)


def test_parse_scrubs_pii_from_raw_but_keeps_business_fields():
    # AVG-dataminimalisatie: no names, addresses or contact details may
    # ever reach orders.raw_json — identity survives only as a hash.
    item = fixture_order(6100000000001)
    rec = parse_order(item)

    for key in ("email", "contact_email", "phone", "shipping_address",
                "billing_address", "client_details"):
        assert key in item          # the fixture really carries the PII
        assert key not in rec.raw   # ...and the scrub removed it
    assert rec.raw["customer"] == {"id": 7000000001, "orders_count": 1}
    assert rec.raw["total_price"] == "29.95"
    assert rec.raw["financial_status"] == "paid"
    assert rec.customer_hash == customer_hash("anna@example.com")


def test_parse_two_unit_shopify_payments_order_exact_cents():
    rec = parse_order(fixture_order(6100000000002))

    assert rec.gross_cents == 5990
    assert rec.vat_cents == 495
    assert rec.net_cents == 5495
    assert rec.units == 2
    assert rec.payment_method == "shopify_payments"
    assert rec.status == "paid"
    assert rec.is_new_customer is True


def test_parse_partial_refund_stays_paid_with_refunded_cents():
    rec = parse_order(fixture_order(6100000000003))

    assert rec.status == "paid"
    assert rec.refunded_cents == 1000  # refunds[].transactions, kind=refund
    # gross/net stay the PRE-refund originals: metrics.order_margin
    # subtracts refunded_cents itself. Shopify's current_total_* fields
    # already deduct refunds and would count them twice.
    assert rec.gross_cents == 2995
    assert rec.vat_cents == 247
    assert rec.net_cents == 2748


def test_parse_fully_refunded_order_gets_refunded_status():
    rec = parse_order(fixture_order(6100000000004))

    assert rec.status == "refunded"
    assert rec.refunded_cents == 2995
    # Original amount kept (like bol's cancelled orders); the status
    # already excludes the order from every metric.
    assert rec.gross_cents == 2995


def test_parse_repeat_customer_shares_hash_and_is_not_new():
    first = parse_order(fixture_order(6100000000001))
    repeat = parse_order(fixture_order(6100000000005))

    assert repeat.customer_hash == first.customer_hash
    assert repeat.is_new_customer is False


def test_parse_shopify_test_order_returns_none():
    assert parse_order(fixture_order(6100000000006)) is None


def test_parse_cancelled_order_counts_as_refunded():
    item = fixture_order(6100000000001)
    item["cancelled_at"] = "2026-06-06T09:00:00+02:00"
    assert parse_order(item).status == "refunded"


def test_parse_taxes_excluded_adds_vat_on_top():
    # Rare non-NL configuration: totals are entered excl. VAT.
    item = fixture_order(6100000000001)
    item["taxes_included"] = False
    item["total_price"] = "27.48"
    item["current_total_price"] = "27.48"
    item["total_tax"] = "2.47"
    item["current_total_tax"] = "2.47"

    rec = parse_order(item)

    assert rec.net_cents == 2748
    assert rec.gross_cents == 2995
    assert rec.vat_cents == 247


def test_parse_without_customer_or_gateway_stays_none():
    item = fixture_order(6100000000001)
    item["customer"] = None
    item["payment_gateway_names"] = []

    rec = parse_order(item)

    assert rec.customer_hash is None
    assert rec.is_new_customer is None
    assert rec.payment_method is None


# ── fetch_orders: params, filtering, pagination ──────────────────────


def test_fetch_orders_queries_updated_at_min_and_skips_test_orders():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"orders": fixture_orders()})

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert len(seen) == 1
    params = seen[0].url.params
    assert seen[0].url.path == "/admin/api/2025-04/orders.json"
    assert seen[0].headers["X-Shopify-Access-Token"] == TOKEN
    assert params["status"] == "any"
    # updated_at_min so refunds/cancellations of older orders are re-fetched.
    assert params["updated_at_min"] == "2026-06-01T00:00:00+02:00"
    assert params["created_at_max"] == "2026-06-30T23:59:59+02:00"
    assert params["limit"] == "250"
    assert params["order"] == "created_at asc"

    ids = [r.extern_id for r in records]
    assert "6100000000006" not in ids  # test order skipped
    assert len(ids) == 5


def test_fetch_orders_drops_orders_created_after_until():
    # updated_at_min can match orders created after the window; the client
    # must drop those even if the server returns them.
    july = fixture_order(6100000000001)
    july["id"] = 6100000000099
    for field in ("created_at", "processed_at", "updated_at"):
        july[field] = "2026-07-02T10:00:00+02:00"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orders": [july, fixture_order(6100000000002)]})

    ids = [r.extern_id for r in _source(handler).fetch_orders(JUNE_1, JUNE_30)]
    assert ids == ["6100000000002"]


def test_pagination_follows_link_next_with_only_page_info():
    seen: list[httpx.Request] = []
    base = f"https://{SHOP}/admin/api/2025-04/orders.json"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("page_info") == "CURSOR2":
            return httpx.Response(200, json={"orders": [fixture_order(6100000000002)]})
        return httpx.Response(
            200,
            json={"orders": [fixture_order(6100000000001)]},
            headers={
                "Link": f'<{base}?limit=250&page_info=CURSOR1>; rel="previous", '
                f'<{base}?limit=250&page_info=CURSOR2>; rel="next"'
            },
        )

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert [r.extern_id for r in records] == ["6100000000001", "6100000000002"]
    assert len(seen) == 2
    second = seen[1].url.params
    assert second["page_info"] == "CURSOR2"
    assert second["limit"] == "250"
    # Shopify rejects filter params next to a page_info cursor.
    assert "updated_at_min" not in second
    assert "status" not in second


# ── rate limits and retries ──────────────────────────────────────────


def test_429_sleeps_retry_after_then_succeeds(sleeps):
    responses = [
        httpx.Response(429, headers={"Retry-After": "1.5"}, json={"errors": "throttled"}),
        httpx.Response(200, json={"orders": [fixture_order(6100000000001)]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert len(records) == 1
    assert responses == []  # both responses consumed
    assert sleeps == [1.5]


def test_429_without_retry_after_uses_default_wait(sleeps):
    responses = [
        httpx.Response(429, json={"errors": "throttled"}),
        httpx.Response(200, json={"orders": []}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [DEFAULT_RETRY_AFTER_SECONDS]


def test_429_keeps_failing_raises_source_error_after_max_retries(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"errors": "throttled"})

    with pytest.raises(SourceError, match="429"):
        list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [1.0] * (MAX_RETRIES - 1)


def test_500_is_retried_with_exponential_backoff(sleeps):
    responses = [
        httpx.Response(503, text="upstream hiccup"),
        httpx.Response(200, json={"orders": []}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [BACKOFF_BASE_SECONDS]


def test_401_raises_credentials_error_without_retry(sleeps):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})

    with pytest.raises(CredentialsError):
        list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert len(seen) == 1  # broken credentials are never retried
    assert sleeps == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [("39/40", [CALL_LIMIT_PAUSE_SECONDS]), ("32/40", [CALL_LIMIT_PAUSE_SECONDS]), ("10/40", [])],
)
def test_call_limit_header_pauses_near_the_bucket_limit(sleeps, header, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"orders": []}, headers={"X-Shopify-Shop-Api-Call-Limit": header}
        )

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == expected


# ── inventory ────────────────────────────────────────────────────────


def test_fetch_inventory_sums_all_variant_quantities():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "id": 1,
                        "title": "Cloudplunge Slaapformule — 60 capsules",
                        "variants": [
                            {"id": 11, "inventory_quantity": 800},
                            {"id": 12, "inventory_quantity": 400},
                        ],
                    },
                    {
                        "id": 2,
                        "title": "Cadeaubon",
                        "variants": [{"id": 21, "inventory_quantity": 300}],
                    },
                ]
            },
        )

    record = _source(handler).fetch_inventory()

    assert record is not None
    assert record.units == 1500
    assert record.source == "shopify"
    assert seen[0].url.path == "/admin/api/2025-04/products.json"
    assert seen[0].url.params["fields"] == "id,title,variants"


def test_fetch_inventory_missing_scope_403_returns_none(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": "Access denied for products scope"})

    with caplog.at_level(logging.WARNING, logger="compass.sources.shopify"):
        assert _source(handler).fetch_inventory() is None
    assert "handmatig" in caplog.text


# ── check_access / verify ────────────────────────────────────────────


def test_check_access_without_credentials_is_offline():
    # No transport, no network: the empty-credentials path returns before any call.
    ok, message = check_access(Settings())
    assert ok is False
    assert "SHOPIFY" in message


def test_check_access_reports_the_shop_name(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/shop.json")
        assert kwargs["headers"]["X-Shopify-Access-Token"] == TOKEN
        return httpx.Response(200, json={"shop": {"name": "Cloudplunge"}})

    monkeypatch.setattr("compass.sources.shopify.httpx.get", fake_get)
    ok, message = check_access(_settings())
    assert ok is True
    assert "Cloudplunge" in message


def test_verify_live_reports_shop_orders_and_inventory():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/shop.json"):
            return httpx.Response(
                200,
                json={
                    "shop": {
                        "name": "Cloudplunge",
                        "myshopify_domain": SHOP,
                        "plan_name": "basic",
                    }
                },
            )
        if path.endswith("/orders.json"):
            return httpx.Response(200, json={"orders": fixture_orders()[:2]})
        if path.endswith("/products.json"):
            return httpx.Response(
                200, json={"products": [{"id": 1, "variants": [{"inventory_quantity": 1200}]}]}
            )
        raise AssertionError(f"unexpected path: {path}")

    report = _source(handler).verify()

    assert report.ok is True
    assert report.mode == "live"
    assert report.source == "shopify"
    assert any("Cloudplunge" in line for line in report.lines)
    assert any("1200 stuks" in line for line in report.lines)


def test_verify_turns_api_failure_into_dutch_diagnosis_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    report = _source(handler).verify()

    assert report.ok is False
    assert report.mode == "live"
    assert any("Shopify" in line for line in report.lines)
