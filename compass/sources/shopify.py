"""Shopify Admin REST API client (read-only): orders + inventory.

Conservative by design: this feeds a daily batch job, not a realtime
system. Rate limiting: honour 429/Retry-After (Shopify's leaky bucket)
and pause proactively when the X-Shopify-Shop-Api-Call-Limit header
says the bucket is nearly full.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlparse

import httpx

from compass.config import Settings
from compass.models import (
    AMSTERDAM,
    InventoryRecord,
    OrderRecord,
    VerifyReport,
    ams_today,
    api_amount_to_cents,
    customer_hash,
    fmt_eur,
)
from compass.sources.base import (
    CredentialsError,
    InventorySource,
    OrdersSource,
    SourceError,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 250  # documented API maximum for orders.json / products.json
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5
DEFAULT_RETRY_AFTER_SECONDS = 2.0  # when a 429 carries no Retry-After header
CALL_LIMIT_SOFT_PCT = 0.8  # of the leaky bucket; above this we pause between calls
CALL_LIMIT_PAUSE_SECONDS = 0.5

CREDENTIALS_HELP = (
    "Shopify weigert de toegang: het token is ongeldig of de Admin API-scopes "
    "(read_orders, read_products, read_inventory) ontbreken. Controleer "
    "SHOPIFY_ACCESS_TOKEN in .env — zie compass/README.md, sectie 'Shopify "
    "custom app aanmaken'."
)

# Fields verify() checks on the newest order; parse_order reads all of these.
KEY_ORDER_FIELDS = (
    "id",
    "created_at",
    "total_price",
    "total_tax",
    "financial_status",
    "line_items",
    "payment_gateway_names",
    "customer",
)


class ShopifySource(OrdersSource, InventorySource):
    """Read-only adapter for the shop's own Shopify Admin API."""

    name = "shopify"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.has_shopify():
            raise CredentialsError(
                "Geen SHOPIFY_SHOP/SHOPIFY_ACCESS_TOKEN gevonden in .env. "
                "Zie compass/README.md, sectie 'Shopify custom app aanmaken'."
            )
        self.settings = settings
        # follow_redirects: a renamed myshopify-domain 301's; harmless for GETs.
        self.client = client or httpx.Client(timeout=30.0, follow_redirects=True)

    # ── OrdersSource interface ────────────────────────────────────────

    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        """Yield orders touched since `since`, created up to `until`.

        Filters on updated_at_min instead of created_at_min on purpose: a
        refund or cancellation UPDATES an old order and must be re-fetched;
        the idempotent upsert makes the extra rows free. Orders created
        after `until` end-of-day are dropped client-side.
        """
        params: dict[str, Any] = {
            "status": "any",
            "updated_at_min": _day_start(since).isoformat(),
            "created_at_max": _day_end(until).isoformat(),
            "limit": PAGE_SIZE,
            "order": "created_at asc",
        }
        url = f"{self.settings.shopify_base_url}/orders.json"
        while True:
            resp = self._get(url, params)
            for item in resp.json().get("orders", []):
                record = parse_order(item)
                if record is None:  # Shopify test order
                    continue
                if record.order_day > until:
                    continue
                yield record
            cursor = _next_page_info(resp)
            if not cursor:
                return
            # Cursor pagination: Shopify rejects filter params next to page_info.
            params = {"limit": PAGE_SIZE, "page_info": cursor}

    def verify(self) -> VerifyReport:
        """Live check for `compass verify`: token works, order fields are
        filled, inventory readable. Reads only — never writes to the database."""
        base = self.settings.shopify_base_url
        lines: list[str] = []
        try:
            shop = self._get(f"{base}/shop.json", None).json().get("shop", {})
            lines.append(
                f"Shop: {shop.get('name', '?')} ({shop.get('myshopify_domain', '?')}, "
                f"plan: {shop.get('plan_name', '?')})"
            )
            month_ago = ams_today() - timedelta(days=30)
            params: dict[str, Any] = {
                "status": "any",
                "created_at_min": _day_start(month_ago).isoformat(),
                "limit": PAGE_SIZE,
                "order": "created_at desc",
            }
            orders = self._get(f"{base}/orders.json", params).json().get("orders", [])
            suffix = " (eerste pagina)" if len(orders) == PAGE_SIZE else ""
            lines.append(f"Orders in de laatste 30 dagen: {len(orders)}{suffix}")
            if orders:
                newest = orders[0]
                missing = [f for f in KEY_ORDER_FIELDS if not newest.get(f)]
                if missing:
                    lines.append(
                        "Let op — deze velden zijn leeg op de nieuwste order: "
                        + ", ".join(missing)
                    )
                else:
                    lines.append(
                        "Alle velden die Compass nodig heeft zijn gevuld op de nieuwste order."
                    )
                record = parse_order(newest)
                if record is not None:
                    lines.append(
                        f"Voorbeeld: order {record.extern_id} van "
                        f"{record.order_day.isoformat()}, {record.units} stuk(s), "
                        f"{fmt_eur(record.gross_cents)} incl. btw."
                    )
            else:
                lines.append(
                    "Geen orders in de laatste 30 dagen — dat kan kloppen bij een nieuwe shop."
                )
            inventory = self.fetch_inventory()
            if inventory is None:
                lines.append(
                    "Voorraad niet leesbaar via Shopify (scope read_products/"
                    "read_inventory ontbreekt?) — handmatig bijhouden kan ook."
                )
            else:
                lines.append(f"Voorraad volgens Shopify: {inventory.units} stuks.")
            return VerifyReport(source=self.name, ok=True, mode="live", lines=lines)
        except SourceError as exc:  # incl. CredentialsError mid-run (token ingetrokken)
            lines.append(f"Fout richting Shopify: {exc}")
            return VerifyReport(source=self.name, ok=False, mode="live", lines=lines)

    # ── InventorySource interface ─────────────────────────────────────

    def fetch_inventory(self) -> InventoryRecord | None:
        """Total sellable units according to Shopify, summed over all variants.

        A missing read_products/read_inventory scope must not fail the run:
        inventory is a nice-to-have that can also be tracked manually.
        """
        params: dict[str, Any] = {"limit": PAGE_SIZE, "fields": "id,title,variants"}
        url = f"{self.settings.shopify_base_url}/products.json"
        units = 0
        try:
            while True:
                resp = self._get(url, params)
                for product in resp.json().get("products", []):
                    for variant in product.get("variants") or []:
                        quantity = variant.get("inventory_quantity")
                        if quantity is not None:
                            units += int(quantity)
                cursor = _next_page_info(resp)
                if not cursor:
                    break
                params = {"limit": PAGE_SIZE, "page_info": cursor, "fields": "id,title,variants"}
        except CredentialsError:
            logger.warning(
                "Shopify voorraad niet leesbaar (scope read_products/read_inventory "
                "ontbreekt?) — voorraad handmatig bijhouden kan ook."
            )
            return None
        except SourceError as exc:
            if getattr(exc, "status_code", None) == 404:
                logger.warning(
                    "Shopify products-endpoint niet gevonden — voorraad handmatig "
                    "bijhouden kan ook."
                )
                return None
            raise
        return InventoryRecord(day=ams_today(), units=units, source="shopify")

    # ── HTTP plumbing ─────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"X-Shopify-Access-Token": self.settings.shopify_access_token}

    def _get(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        last_error: SourceError | None = None
        wait = 0.0
        for attempt in range(MAX_RETRIES):
            if attempt:
                logger.warning("Retry %d/%d in %.1fs", attempt, MAX_RETRIES - 1, wait)
                time.sleep(wait)
            try:
                resp = self.client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = SourceError(f"Netwerkfout richting Shopify: {exc}")
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                continue

            self._respect_call_limit(resp)

            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                # Shopify's leaky bucket: wait exactly as long as it asks.
                wait = _retry_after_seconds(resp)
                last_error = SourceError("Shopify rate limit (429) bleef aanhouden")
                continue
            if resp.status_code in (401, 403):
                raise CredentialsError(CREDENTIALS_HELP)
            if resp.status_code >= 500:
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                last_error = SourceError(
                    f"Shopify tijdelijk niet beschikbaar (status {resp.status_code})"
                )
                continue
            error = SourceError(
                f"Shopify API-fout (status {resp.status_code}): {resp.text[:300]}"
            )
            # fetch_inventory downgrades a 404 to a warning; give it the status.
            error.status_code = resp.status_code  # type: ignore[attr-defined]
            raise error
        raise last_error or SourceError("Shopify API bleef falen na retries")

    def _respect_call_limit(self, resp: httpx.Response) -> None:
        """Parse X-Shopify-Shop-Api-Call-Limit ('39/40') and voluntarily
        pause near the bucket limit instead of provoking a 429."""
        header = resp.headers.get("x-shopify-shop-api-call-limit", "")
        used_txt, _, limit_txt = header.partition("/")
        try:
            used, limit = int(used_txt), int(limit_txt)
        except ValueError:
            return
        if limit > 0 and used / limit >= CALL_LIMIT_SOFT_PCT:
            logger.warning(
                "Shopify call-limit op %s — pauzeer %.1fs", header, CALL_LIMIT_PAUSE_SECONDS
            )
            time.sleep(CALL_LIMIT_PAUSE_SECONDS)


def check_access(settings: Settings) -> tuple[bool, str]:
    """Lightweight access check for `compass status` (GET /shop.json)."""
    if not settings.has_shopify():
        return False, "geen SHOPIFY_SHOP/SHOPIFY_ACCESS_TOKEN in .env"
    try:
        resp = httpx.get(
            f"{settings.shopify_base_url}/shop.json",
            headers={"X-Shopify-Access-Token": settings.shopify_access_token},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return False, f"netwerkfout richting Shopify: {exc}"
    if resp.status_code == 200:
        shop = resp.json().get("shop", {})
        name = shop.get("name") or shop.get("myshopify_domain") or "?"
        return True, f"toegang ok (shop: {name})"
    if resp.status_code in (401, 403):
        return False, CREDENTIALS_HELP
    return False, f"Shopify API-fout (status {resp.status_code})"


# ── module helpers ───────────────────────────────────────────────────


def _day_start(day: date) -> datetime:
    """00:00 Amsterdam wall clock of a calendar day, timezone-aware."""
    return datetime(day.year, day.month, day.day, tzinfo=AMSTERDAM)


def _day_end(day: date) -> datetime:
    """End of the Amsterdam calendar day. Whole seconds are enough:
    Shopify timestamps carry no sub-second precision."""
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=AMSTERDAM)


def _retry_after_seconds(resp: httpx.Response) -> float:
    """Shopify's Retry-After is fractional seconds; default when absent."""
    try:
        return max(float(resp.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS


def _next_page_info(resp: httpx.Response) -> str | None:
    """The rel="next" cursor from Shopify's Link header, if any."""
    header = resp.headers.get("link", "")
    for part in header.split(","):
        if 'rel="next"' not in part:
            continue
        start, end = part.find("<"), part.find(">")
        if start < 0 or end < 0:
            continue
        query = urlparse(part[start + 1 : end]).query
        return dict(parse_qsl(query)).get("page_info")
    return None


def parse_order(item: dict[str, Any]) -> OrderRecord | None:
    """Normalize one orders.json item; None for Shopify test orders.

    Money comes from the ORIGINAL total_price/total_tax on purpose: refunds
    are carried separately in refunded_cents, and metrics.order_margin
    subtracts them — Shopify's current_total_* fields already deduct
    refunds, so using those would count every refund twice. The VAT split
    is Shopify's own so the cents match what the owners see in the admin.
    `taxes_included` is True in every NL shop, but the False case (prices
    entered excl. VAT) is handled so a config change can't corrupt data.
    """
    if item.get("test"):
        return None

    listed = api_amount_to_cents(item.get("total_price") or item.get("current_total_price")) or 0
    vat = api_amount_to_cents(item.get("total_tax") or item.get("current_total_tax")) or 0
    if item.get("taxes_included", True):
        gross, net = listed, listed - vat
    else:
        gross, net = listed + vat, listed

    units = sum(int(li.get("quantity") or 0) for li in item.get("line_items") or [])

    refunded = 0
    for refund in item.get("refunds") or []:
        for tx in refund.get("transactions") or []:
            if tx.get("kind") in ("refund", "void"):
                refunded += api_amount_to_cents(tx.get("amount")) or 0

    status = "paid"
    if item.get("financial_status") in ("refunded", "voided") or item.get("cancelled_at"):
        status = "refunded"

    customer = item.get("customer") or {}
    identifier = customer.get("email") or (
        str(customer["id"]) if customer.get("id") is not None else None
    )
    orders_count = customer.get("orders_count")
    is_new = (orders_count == 1) if orders_count is not None else None

    gateways = item.get("payment_gateway_names") or []
    payment_method = gateways[0].lower() if gateways and gateways[0] else None

    ordered_at = datetime.fromisoformat(item.get("processed_at") or item["created_at"])

    return OrderRecord(
        extern_id=str(item["id"]),
        channel="shopify",
        ordered_at=ordered_at,
        gross_cents=gross,
        units=units,
        net_cents=net,
        vat_cents=vat,
        customer_hash=customer_hash(identifier),
        is_new_customer=is_new,
        payment_method=payment_method,
        status=status,
        refunded_cents=refunded,
        raw=scrub_raw(item),
    )


# AVG-dataminimalisatie (hard spec rule): the stored raw payload keeps every
# business field but never names, addresses or contact details.
_PII_KEYS = frozenset(
    {
        "email",
        "contact_email",
        "phone",
        "billing_address",
        "shipping_address",
        "client_details",
        "payment_details",
        "customer_locale",
        "note",
        "browser_ip",
    }
)


def scrub_raw(item: dict[str, Any]) -> dict[str, Any]:
    """Strip PII before the payload is persisted in orders.raw_json.

    The customer object shrinks to the two fields Compass actually uses
    (id, orders_count) — identity itself only ever lives on as a hash.
    """
    clean = {k: v for k, v in item.items() if k not in _PII_KEYS}
    customer = item.get("customer")
    if isinstance(customer, dict):
        clean["customer"] = {
            k: customer[k] for k in ("id", "orders_count") if k in customer
        }
    return clean
