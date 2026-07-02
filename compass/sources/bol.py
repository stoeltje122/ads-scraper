"""bol Retailer API client (read-only): orders.

Conservative by design: this feeds a daily batch job, not a realtime
system. Auth is OAuth2 client-credentials with a cached bearer token;
rate limiting honours 429/Retry-After and backs off exponentially on 5xx.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Iterator

import httpx

from compass.config import BOL_API_BASE, BOL_TOKEN_URL, Settings
from compass.models import OrderRecord, VerifyReport, ams_day, api_amount_to_cents, fmt_eur
from compass.sources.base import CredentialsError, OrdersSource, SourceError

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5
DEFAULT_RETRY_AFTER_SECONDS = 5.0  # when a 429 carries no Retry-After header
MAX_PAGES = 500  # safety guard; ~12.500 orders per fetch is far beyond one SKU
TOKEN_REFRESH_MARGIN_SECONDS = 60  # refresh early so a token never dies mid-pagination

# bol versions its API via the Accept header; one place to bump on deprecation.
ACCEPT_HEADER = "application/vnd.retailer.v10+json"

CREDENTIALS_HELP = (
    "bol weigert de toegang: controleer BOL_CLIENT_ID en BOL_CLIENT_SECRET "
    "in .env — zie compass/README.md, sectie 'bol Retailer API'."
)


class BolSource(OrdersSource):
    """Read-only adapter for the bol Retailer API of our own seller account."""

    name = "bol"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.has_bol():
            raise CredentialsError(
                "Geen BOL_CLIENT_ID/BOL_CLIENT_SECRET gevonden in .env. "
                "Zie compass/README.md, sectie 'bol Retailer API'."
            )
        self.settings = settings
        self.client = client or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ── OrdersSource interface ────────────────────────────────────────

    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        """Yield orders placed within the inclusive [since, until] window.

        The orders list is newest-first and cannot be filtered by date, so
        paging simply stops at the first stub older than `since`. The list
        endpoint only returns stubs; the full money/cancellation picture
        needs a detail fetch per order — fine at this shop's order volume.
        """
        for page in range(1, MAX_PAGES + 1):
            data = self._get(
                f"{BOL_API_BASE}/retailer/orders",
                {"status": "ALL", "fulfilment-method": "ALL", "page": page},
            )
            stubs = data.get("orders") or []
            if not stubs:
                return
            for stub in stubs:
                day = ams_day(datetime.fromisoformat(stub["orderPlacedDateTime"]))
                if day < since:
                    return  # newest-first: everything after this is older still
                if day > until:
                    continue
                detail = self._get(f"{BOL_API_BASE}/retailer/orders/{stub['orderId']}", None)
                yield parse_order_detail(detail)
        logger.warning("bol paging afgebroken na %d pagina's (veiligheidslimiet).", MAX_PAGES)

    def verify(self) -> VerifyReport:
        """Live check for `compass verify`: credentials work, orders are
        readable and parseable. Reads only — never writes to the database."""
        lines: list[str] = []
        try:
            self._access_token()
            lines.append("Inloggen met client credentials gelukt.")
            # No status filter: the default OPEN view is the natural health check.
            data = self._get(f"{BOL_API_BASE}/retailer/orders", {"page": 1})
            stubs = data.get("orders") or []
            lines.append(f"Open orders op dit moment: {len(stubs)}.")
            if stubs:
                detail = self._get(
                    f"{BOL_API_BASE}/retailer/orders/{stubs[0]['orderId']}", None
                )
                record = parse_order_detail(detail)
                lines.append(
                    f"Voorbeeld: order {record.extern_id} van "
                    f"{record.order_day.isoformat()}, {record.units} stuk(s), "
                    f"{fmt_eur(record.gross_cents)} incl. btw."
                )
            else:
                lines.append(
                    "Geen open orders — dat kan kloppen als alles verzonden is "
                    "of je net begint op bol."
                )
            return VerifyReport(source=self.name, ok=True, mode="live", lines=lines)
        except SourceError as exc:  # incl. CredentialsError (client credentials fout)
            lines.append(f"Fout richting bol: {exc}")
            return VerifyReport(source=self.name, ok=False, mode="live", lines=lines)

    # ── HTTP plumbing ─────────────────────────────────────────────────

    def _access_token(self) -> str:
        """Cached client-credentials token, refreshed with a safety margin.

        bol tokens live ~5 minutes; refreshing when less than 60s is left
        means a token can never expire between two calls of one run.
        """
        if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        try:
            resp = self.client.post(
                BOL_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.settings.bol_client_id, self.settings.bol_client_secret),
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"Netwerkfout richting bol login: {exc}")
        if resp.status_code in (401, 403):
            raise CredentialsError(CREDENTIALS_HELP)
        if resp.status_code != 200:
            raise SourceError(
                f"bol token-fout (status {resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + float(data.get("expires_in", 0))
        return self._token

    def _get(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        last_error: SourceError | None = None
        wait = 0.0
        for attempt in range(MAX_RETRIES):
            if attempt:
                logger.warning("Retry %d/%d in %.1fs", attempt, MAX_RETRIES - 1, wait)
                time.sleep(wait)
            headers = {
                "Accept": ACCEPT_HEADER,
                "Authorization": f"Bearer {self._access_token()}",
            }
            try:
                resp = self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = SourceError(f"Netwerkfout richting bol: {exc}")
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                continue

            if resp.status_code == 200:
                return resp.json() if resp.content else {}
            if resp.status_code == 429:
                # bol's rate limiter says exactly how long to wait.
                wait = _retry_after_seconds(resp)
                last_error = SourceError("bol rate limit (429) bleef aanhouden")
                continue
            if resp.status_code in (401, 403):
                # The margin makes mid-run expiry unlikely: treat as revoked access.
                raise CredentialsError(CREDENTIALS_HELP)
            if resp.status_code >= 500:
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                last_error = SourceError(
                    f"bol tijdelijk niet beschikbaar (status {resp.status_code})"
                )
                continue
            raise SourceError(
                f"bol API-fout (status {resp.status_code}): {resp.text[:300]}"
            )
        raise last_error or SourceError("bol API bleef falen na retries")


# ── module helpers ───────────────────────────────────────────────────


def _retry_after_seconds(resp: httpx.Response) -> float:
    """bol's Retry-After is whole seconds; default when absent."""
    try:
        return max(float(resp.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS


def _cancelled_quantity(item: dict[str, Any], quantity: int) -> int:
    """Units of this line that are (or are about to be) cancelled.

    A pending cancellationRequest counts as the whole line (conservative:
    bol honours nearly all of them); otherwise the executed
    quantityCancelled, capped at the line quantity."""
    if item.get("cancellationRequest"):
        return quantity
    cancelled = item.get("quantityCancelled")
    if not cancelled:
        return 0
    return min(int(cancelled), quantity)


def parse_order_detail(detail: dict[str, Any]) -> OrderRecord:
    """Normalize one /retailer/orders/{orderId} response into an OrderRecord.

    bol has no refunds endpoint here; cancellations are its refunds, and
    they map onto the same columns Shopify uses so gross/refunded mean the
    same thing on every channel: gross_cents keeps the ORIGINAL order
    total (matches the bol seller console), the cancelled value goes into
    refunded_cents (partial cancellations included), and units counts only
    the kept units — COGS is charged per shipped unit. A fully cancelled
    order becomes status 'refunded' with its original units. bol masks
    buyer identity and splits no VAT: customer_hash/net/vat stay None —
    the cost model derives the VAT and an unidentifiable customer
    deliberately counts as new.
    """
    gross_all = units_all = 0
    cancelled_value = units_kept = 0
    for item in detail.get("orderItems") or []:
        quantity = int(item.get("quantity") or 0)
        unit_cents = api_amount_to_cents(item.get("unitPrice")) or 0
        cancelled = _cancelled_quantity(item, quantity)
        gross_all += unit_cents * quantity
        units_all += quantity
        cancelled_value += unit_cents * cancelled
        units_kept += quantity - cancelled

    if units_all and units_kept == 0:
        status, units = "refunded", units_all
    else:
        status, units = "paid", units_kept
    gross = gross_all

    return OrderRecord(
        extern_id=str(detail["orderId"]),
        channel="bol",
        ordered_at=datetime.fromisoformat(detail["orderPlacedDateTime"]),
        gross_cents=gross,
        units=units,
        net_cents=None,
        vat_cents=None,
        customer_hash=None,
        is_new_customer=None,
        payment_method=None,  # bol collects payment; its commission is the cost
        status=status,
        refunded_cents=cancelled_value,
        raw=scrub_raw(detail),
    )


# AVG-dataminimalisatie (hard spec rule): bol order details carry the
# buyer's name/address in these blocks; none of it may be persisted.
_PII_KEYS = frozenset({"shipmentDetails", "billingDetails", "pickupPoint"})


def scrub_raw(detail: dict[str, Any]) -> dict[str, Any]:
    """Strip PII before the payload is persisted in orders.raw_json."""
    return {k: v for k, v in detail.items() if k not in _PII_KEYS}
