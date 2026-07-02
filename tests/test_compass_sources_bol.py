"""BolSource: client-credentials token caching/refresh, exact-cents parsing
of the committed fixture order details (incl. cancellations) and the
newest-first paging/rate-limit behaviour, all offline via httpx.MockTransport."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from compass.config import Settings
from compass.sources.base import CredentialsError, SourceError
from compass.sources.bol import (
    ACCEPT_HEADER,
    BACKOFF_BASE_SECONDS,
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_RETRIES,
    BolSource,
    parse_order_detail,
)

CLIENT_ID = "bol-client-id"
CLIENT_SECRET = "bol-client-secret"
JUNE_1 = date(2026, 6, 1)
JUNE_30 = date(2026, 6, 30)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "compass" / "bol_orders.json"


def _settings() -> Settings:
    return Settings(bol_client_id=CLIENT_ID, bol_client_secret=CLIENT_SECRET)


def _source(handler) -> BolSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BolSource(_settings(), client=client)


def fixture_details() -> list[dict]:
    """Fresh copies of the committed fixture order details (safe to mutate)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["orders"]


def fixture_detail(order_id: str) -> dict:
    return next(o for o in fixture_details() if o["orderId"] == order_id)


def _stub(detail: dict) -> dict:
    """The reduced shape the /retailer/orders list returns per order."""
    return {
        "orderId": detail["orderId"],
        "orderPlacedDateTime": detail["orderPlacedDateTime"],
        "orderItems": [
            {"orderItemId": item["orderItemId"], "quantity": item["quantity"]}
            for item in detail["orderItems"]
        ],
    }


class BolHandler:
    """MockTransport handler serving the token endpoint plus retailer pages."""

    def __init__(self, pages: list[list[dict]] | None = None, expires_in: int = 299):
        self.pages = pages if pages is not None else [[]]
        self.expires_in = expires_in
        self.token_requests: list[httpx.Request] = []
        self.api_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.bol.com":
            self.token_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{len(self.token_requests)}",
                    "token_type": "Bearer",
                    "expires_in": self.expires_in,
                    "scope": "RETAILER",
                },
            )
        self.api_requests.append(request)
        path = request.url.path
        if path == "/retailer/orders":
            page = int(request.url.params.get("page", "1"))
            stubs = self.pages[page - 1] if page <= len(self.pages) else []
            return httpx.Response(200, json={"orders": stubs})
        if path.startswith("/retailer/orders/"):
            return httpx.Response(200, json=fixture_detail(path.rsplit("/", 1)[1]))
        raise AssertionError(f"unexpected path: {path}")

    def list_pages_requested(self) -> list[str]:
        return [
            r.url.params["page"] for r in self.api_requests if r.url.path == "/retailer/orders"
        ]

    def detail_paths_requested(self) -> list[str]:
        return [
            r.url.path for r in self.api_requests if r.url.path.startswith("/retailer/orders/")
        ]


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture time.sleep calls inside the bol module (no real waiting)."""
    calls: list[float] = []
    monkeypatch.setattr("compass.sources.bol.time.sleep", calls.append)
    return calls


# ── constructor / credentials ────────────────────────────────────────


def test_constructor_without_credentials_raises_credentials_error():
    with pytest.raises(CredentialsError) as excinfo:
        BolSource(Settings())
    assert "BOL_CLIENT_ID" in str(excinfo.value)


# ── parse_order_detail: exact cents and cancellation rules ───────────


def test_parse_single_unit_order_exact_cents_and_identity():
    detail = fixture_detail("2550001001")
    rec = parse_order_detail(detail)

    assert rec.extern_id == "2550001001"
    assert rec.channel == "bol"
    assert rec.gross_cents == 2995
    assert rec.units == 1
    assert rec.net_cents is None  # bol splits no VAT; the cost model derives it
    assert rec.vat_cents is None
    assert rec.customer_hash is None  # bol masks identity → counts as new customer
    assert rec.is_new_customer is None
    assert rec.payment_method is None  # bol collects payment itself
    assert rec.status == "paid"
    assert rec.refunded_cents == 0
    assert rec.ordered_at == datetime.fromisoformat("2026-06-10T09:12:00+02:00")
    assert rec.order_day == date(2026, 6, 10)
    # AVG: shipment/billing details (name, address) are scrubbed from raw.
    assert "shipmentDetails" in detail and "billingDetails" in detail
    assert "shipmentDetails" not in rec.raw
    assert "billingDetails" not in rec.raw
    assert rec.raw["orderId"] == detail["orderId"]


def test_parse_two_unit_order_multiplies_unit_price():
    rec = parse_order_detail(fixture_detail("2550001002"))

    assert rec.gross_cents == 5990
    assert rec.units == 2
    assert rec.status == "paid"


def test_parse_cancelled_item_next_to_kept_item_moves_to_refunded():
    # Item 2 has a pending cancellationRequest: the ORIGINAL total stays in
    # gross (matches the bol console), the cancelled value moves to
    # refunded_cents (same semantics as a Shopify refund) and only the
    # kept unit counts for COGS.
    rec = parse_order_detail(fixture_detail("2550001003"))

    assert rec.gross_cents == 5990
    assert rec.refunded_cents == 2995
    assert rec.units == 1
    assert rec.status == "paid"


def test_parse_fully_cancelled_order_is_refunded_with_original_total():
    # quantityCancelled == quantity: the row keeps its original total so it
    # stays meaningful, but status 'refunded' zeroes the margin.
    rec = parse_order_detail(fixture_detail("2550001004"))

    assert rec.status == "refunded"
    assert rec.gross_cents == 2995
    assert rec.refunded_cents == 2995
    assert rec.units == 1


def test_parse_all_items_cancelled_via_request_is_refunded_too():
    detail = fixture_detail("2550001002")
    detail["orderItems"][0]["cancellationRequest"] = True

    rec = parse_order_detail(detail)

    assert rec.status == "refunded"
    assert rec.gross_cents == 5990  # original two-unit total kept
    assert rec.refunded_cents == 5990
    assert rec.units == 2


def test_parse_partially_cancelled_quantity_refunds_that_unit():
    # 1 of 2 units cancelled: bol never pays that unit out, so its value
    # belongs in refunded_cents and its COGS must not be charged.
    detail = fixture_detail("2550001002")
    detail["orderItems"][0]["quantityCancelled"] = 1

    rec = parse_order_detail(detail)

    assert rec.status == "paid"
    assert rec.gross_cents == 5990
    assert rec.refunded_cents == 2995
    assert rec.units == 1


# ── OAuth token flow ─────────────────────────────────────────────────


def test_token_request_uses_basic_auth_and_client_credentials_grant():
    handler = BolHandler(pages=[[_stub(fixture_detail("2550001001"))]])

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert [r.extern_id for r in records] == ["2550001001"]
    assert len(handler.token_requests) == 1
    token_request = handler.token_requests[0]
    assert str(token_request.url) == "https://login.bol.com/token"
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_request.headers["Authorization"] == f"Basic {expected}"
    assert token_request.content.decode() == "grant_type=client_credentials"


def test_token_is_cached_across_calls_while_valid():
    handler = BolHandler(
        pages=[[_stub(fixture_detail("2550001002")), _stub(fixture_detail("2550001001"))]]
    )
    source = _source(handler)

    list(source.fetch_orders(JUNE_1, JUNE_30))

    # List page 1 + two detail calls + the empty page 2 probe, all on the
    # same cached token.
    assert len(handler.api_requests) == 4
    assert len(handler.token_requests) == 1
    assert all(r.headers["Authorization"] == "Bearer token-1" for r in handler.api_requests)


def test_nearly_expired_token_is_refreshed_before_the_next_call():
    # expires_in below the 60s refresh margin → every new call refreshes.
    handler = BolHandler(pages=[[]], expires_in=30)
    source = _source(handler)

    list(source.fetch_orders(JUNE_1, JUNE_30))
    list(source.fetch_orders(JUNE_1, JUNE_30))

    assert len(handler.token_requests) == 2
    assert handler.api_requests[-1].headers["Authorization"] == "Bearer token-2"


def test_401_on_token_fetch_raises_credentials_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "login.bol.com"
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(CredentialsError) as excinfo:
        list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert "BOL_CLIENT_ID" in str(excinfo.value)


# ── fetch_orders: headers, params, paging ────────────────────────────


def test_fetch_orders_sends_v10_accept_and_bearer_headers():
    handler = BolHandler(pages=[[_stub(fixture_detail("2550001001"))]])

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    first = handler.api_requests[0]
    assert first.headers["Accept"] == ACCEPT_HEADER
    assert first.headers["Accept"] == "application/vnd.retailer.v10+json"
    assert first.headers["Authorization"] == "Bearer token-1"
    params = first.url.params
    assert params["status"] == "ALL"
    assert params["fulfilment-method"] == "ALL"
    assert params["page"] == "1"


def test_paging_stops_at_the_first_order_older_than_since():
    stub_old = {
        "orderId": "2549990000",
        "orderPlacedDateTime": "2026-05-28T10:00:00+02:00",
        "orderItems": [],
    }
    handler = BolHandler(
        pages=[
            [_stub(fixture_detail("2550001004")), _stub(fixture_detail("2550001003"))],
            [_stub(fixture_detail("2550001002")), stub_old],
            [_stub(fixture_detail("2550001001"))],  # must never be requested
        ]
    )

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert [r.extern_id for r in records] == ["2550001004", "2550001003", "2550001002"]
    # The list is newest-first: the May stub stops paging (no page 3) and
    # gets no detail fetch of its own.
    assert handler.list_pages_requested() == ["1", "2"]
    assert "/retailer/orders/2549990000" not in handler.detail_paths_requested()


def test_orders_after_until_are_skipped_without_a_detail_fetch():
    stub_july = {
        "orderId": "2550009999",
        "orderPlacedDateTime": "2026-07-01T09:00:00+02:00",
        "orderItems": [],
    }
    handler = BolHandler(pages=[[stub_july, _stub(fixture_detail("2550001004"))]])

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert [r.extern_id for r in records] == ["2550001004"]
    assert "/retailer/orders/2550009999" not in handler.detail_paths_requested()


def test_empty_orders_page_ends_pagination():
    handler = BolHandler(pages=[[_stub(fixture_detail("2550001001"))], []])

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert len(records) == 1
    assert handler.list_pages_requested() == ["1", "2"]


# ── rate limits and retries ──────────────────────────────────────────


def _throttled_then(pages_handler: BolHandler, first_response: httpx.Response):
    """Serve one error for the first retailer call, then delegate."""
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "login.bol.com" and not state["failed"]:
            state["failed"] = True
            return first_response
        return pages_handler(request)

    return handler


def test_429_sleeps_retry_after_then_succeeds(sleeps):
    inner = BolHandler(pages=[[_stub(fixture_detail("2550001001"))]])
    handler = _throttled_then(
        inner, httpx.Response(429, headers={"Retry-After": "2"}, json={"title": "Too many requests"})
    )

    records = list(_source(handler).fetch_orders(JUNE_1, JUNE_30))

    assert [r.extern_id for r in records] == ["2550001001"]
    assert sleeps == [2.0]


def test_429_without_retry_after_uses_default_wait(sleeps):
    inner = BolHandler(pages=[[]])
    handler = _throttled_then(inner, httpx.Response(429, json={"title": "Too many requests"}))

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [DEFAULT_RETRY_AFTER_SECONDS]


def test_429_keeps_failing_raises_source_error_after_max_retries(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.bol.com":
            return httpx.Response(
                200, json={"access_token": "token-1", "token_type": "Bearer", "expires_in": 299}
            )
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"title": "throttled"})

    with pytest.raises(SourceError, match="429"):
        list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [1.0] * (MAX_RETRIES - 1)


def test_500_is_retried_with_exponential_backoff(sleeps):
    inner = BolHandler(pages=[[]])
    handler = _throttled_then(inner, httpx.Response(503, text="upstream hiccup"))

    list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == [BACKOFF_BASE_SECONDS]


def test_401_on_the_api_raises_credentials_error_without_retry(sleeps):
    inner = BolHandler(pages=[[]])
    handler = _throttled_then(inner, httpx.Response(401, json={"title": "Unauthorized"}))

    with pytest.raises(CredentialsError):
        list(_source(handler).fetch_orders(JUNE_1, JUNE_30))
    assert sleeps == []


# ── verify ───────────────────────────────────────────────────────────


def test_verify_live_reports_open_orders_and_a_parsed_example():
    handler = BolHandler(
        pages=[[_stub(fixture_detail("2550001001")), _stub(fixture_detail("2550001002"))]]
    )

    report = _source(handler).verify()

    assert report.ok is True
    assert report.mode == "live"
    assert report.source == "bol"
    assert any("Open orders" in line and "2" in line for line in report.lines)
    assert any("2550001001" in line and "€ 29,95" in line for line in report.lines)


def test_verify_turns_credentials_problem_into_dutch_diagnosis_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    report = _source(handler).verify()

    assert report.ok is False
    assert report.mode == "live"
    assert any("bol" in line for line in report.lines)
