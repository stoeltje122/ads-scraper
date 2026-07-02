"""Normalized data records passed between sources, collector and storage.

Money policy: every amount is integer cents (EUR) everywhere inside
Compass; euros only exist at the display edge (fmt_eur) and at the parse
edge (parse_eur_to_cents). Floats never carry money.

Day policy: ams_day()/ams_today() define the one day boundary for all of
Compass — the Europe/Amsterdam calendar day. Deliberately different from
AdScout's UTC policy: Shopify, bol and Meta all report to the owners in
the account timezone (Amsterdam), and the finance numbers must match what
they see there. Never call date.today() in business logic.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

CHANNELS = ("shopify", "bol")

# Shown wherever a signal or advice appears (dashboard, report, CLI).
DISCLAIMER = (
    "Dit zijn vuistregels op basis van jullie eigen cijfers, "
    "geen financieel advies — jullie beslissen."
)
NO_BOOKKEEPING = (
    "Indicatief overzicht op basis van het kostenmodel — geen boekhouding."
)


def ams_today() -> date:
    """Today as the owners experience it (Europe/Amsterdam)."""
    return datetime.now(AMSTERDAM).date()


def ams_day(moment: datetime) -> date:
    """The Amsterdam calendar day of a (timezone-aware) moment.

    Naive datetimes are assumed to already be Amsterdam wall-clock time.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(AMSTERDAM).date()


def customer_hash(identifier: str | None) -> str | None:
    """One-way hash for customer identity (AVG-dataminimalisatie).

    We only ever need "same customer as before?"; a sha256 of the
    normalized identifier answers that without storing names or e-mail.
    """
    if not identifier or not identifier.strip():
        return None
    normalized = identifier.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── money helpers ────────────────────────────────────────────────────


# '1.005' with no comma is a Dutch thousands notation (€1005), except when
# it starts with '0.' — then it can only be a decimal fraction.
_THOUSANDS_RE = re.compile(r"^\d{1,3}(\.\d{3})+$")


def parse_eur_to_cents(value: str | float | int | None) -> int | None:
    """Parse a source amount ('29.95', '29,95', '1.234,56', 29.95) into
    integer cents.

    The one conversion used by every adapter and the CSV importer, so
    rounding can never drift between sources. None/empty stays None.
    Halves round away from zero ('1.005' → 101 cents).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        sign = -1 if value < 0 else 1
        return sign * int(abs(value) * 100 + 0.5)
    text = value.strip().replace("€", "").replace(" ", "")
    if not text:
        return None
    negative = text.startswith("-")
    text = text.lstrip("+-")
    if "," in text:
        # NL style: dots are thousands separators, comma is the decimal.
        text = text.replace(".", "").replace(",", ".")
    elif _THOUSANDS_RE.match(text) and not text.startswith("0."):
        text = text.replace(".", "")
    euros_txt, _, decimals = text.partition(".")
    cents = int(euros_txt or "0") * 100 + int((decimals + "00")[:2])
    if len(decimals) > 2 and decimals[2] >= "5":
        cents += 1
    return -cents if negative else cents


def api_amount_to_cents(value: str | float | int | None) -> int | None:
    """Parse an API decimal amount ('123.45', 123.45) into integer cents.

    For machine formats only (Shopify, Meta, bol): the dot is always the
    decimal point, never a Dutch thousands separator — '1.005' here means
    €1,005 → 101 cents. Human/CSV input goes through parse_eur_to_cents.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    cents = Decimal(str(value).strip()) * 100
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_eur(cents: int | None, *, decimals: int = 2) -> str:
    """Display cents as Dutch-formatted euros: 123456 → '€ 1.234,56'."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    euros, rest = divmod(cents, 100)
    euros_txt = f"{euros:,}".replace(",", ".")
    if decimals == 0:
        return f"{sign}€ {euros_txt}"
    return f"{sign}€ {euros_txt},{rest:02d}"


# ── records ──────────────────────────────────────────────────────────


@dataclass
class OrderRecord:
    """One order from a sales channel, normalized. `raw` keeps the payload."""

    extern_id: str
    channel: str                      # 'shopify' | 'bol'
    ordered_at: datetime              # tz-aware moment of the order
    gross_cents: int                  # incl. VAT, after discounts
    units: int = 1
    net_cents: int | None = None      # excl. VAT, when the source provides it
    vat_cents: int | None = None
    customer_hash: str | None = None
    is_new_customer: bool | None = None
    payment_method: str | None = None
    status: str = "paid"              # 'paid' | 'refunded'
    refunded_cents: int = 0           # partial refunds, incl. VAT
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def order_day(self) -> date:
        return ams_day(self.ordered_at)

    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)


@dataclass
class AdSpendRecord:
    """One campaign-day from Meta Insights. Meta's own purchase numbers are
    kept clearly apart (meta_*): they are Meta's attribution claim, never
    'the revenue'."""

    day: date
    campaign_id: str
    campaign_name: str | None = None
    spend_cents: int = 0
    impressions: int | None = None
    clicks: int | None = None
    meta_purchases: int | None = None
    meta_purchase_value_cents: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)


@dataclass
class InventoryRecord:
    day: date
    units: int
    source: str = "manual"            # 'shopify' | 'manual' | 'fixture'


@dataclass
class Signal:
    """An advisory signal. Compass advises; it never acts."""

    type: str                         # scale_up / scale_down / reorder / spend_anomaly
    day: date
    message: str                      # one-line advice, plain Dutch
    explanation: str = ""             # the numbers behind it, plain Dutch
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "new"               # 'new' | 'seen'
    id: int | None = None             # set when loaded from the database


@dataclass(frozen=True)
class PaymentFee:
    """Transaction cost for one payment method: pct of gross + fixed."""

    pct: float = 0.0                  # fraction of the gross amount
    fixed_cents: int = 0


@dataclass
class CostModel:
    """One version of the cost model; valid from `valid_from` until the
    next version's valid_from. Margins are always computed with the
    version that was valid on the order day."""

    valid_from: date
    cogs_per_unit_cents: int
    shipping_per_order_cents: int
    fee_pct: float = 0.0
    fee_fixed_cents: int = 0
    payment_fees: dict[str, PaymentFee] = field(default_factory=dict)
    bol_commission_pct: float = 0.0
    vat_rate: float = 0.09
    fixed_month_cents: int = 0
    lead_time_days: int = 30
    safety_factor: float = 1.3
    note: str | None = None
    id: int | None = None             # set when loaded from the database

    def fee_for(self, payment_method: str | None) -> PaymentFee:
        """The fee schedule for a payment method, falling back to the
        model's default pct/fixed."""
        if payment_method:
            override = self.payment_fees.get(payment_method.strip().lower())
            if override is not None:
                return override
        return PaymentFee(pct=self.fee_pct, fixed_cents=self.fee_fixed_cents)

    def payment_fees_json(self) -> str:
        return json.dumps(
            {
                method: {"pct": fee.pct, "fixed_cents": fee.fixed_cents}
                for method, fee in sorted(self.payment_fees.items())
            },
            ensure_ascii=False,
        )

    @staticmethod
    def parse_payment_fees(raw: str | None) -> dict[str, PaymentFee]:
        if not raw:
            return {}
        data = json.loads(raw)
        return {
            method: PaymentFee(
                pct=float(value.get("pct", 0.0)),
                fixed_cents=int(value.get("fixed_cents", 0)),
            )
            for method, value in data.items()
        }


@dataclass
class VerifyReport:
    """Result of one adapter's verify(): plain-language lines a founder
    can read, plus a machine-readable ok/mode."""

    source: str                       # 'shopify' | 'meta' | 'bol'
    ok: bool
    mode: str                         # 'live' | 'fixture' | 'geen-credentials'
    lines: list[str] = field(default_factory=list)
