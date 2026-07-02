"""The one place every Compass formula lives.

Every number on the dashboard, in the weekly report and behind the
signals comes from a function in this module — nothing recomputes a
margin or a ROAS ad hoc. Each formula has unit tests with hand-computed
examples in tests/compass/test_metrics.py.

Definitions (fixed, per the Compass spec):
  * omzet excl. btw     = omzet incl. btw / (1 + btw-tarief)
  * contributiemarge    = omzet excl. btw − COGS·units − verzendkosten
                          − transactiekosten − bol-commissie (bol only)
  * marge-ratio         = contributiemarge / omzet excl. btw
  * break-even ROAS     = 1 / marge-ratio
  * MER (blended)       = omzet excl. btw / ad spend        (same period)
  * ROAS volgens Meta   = Meta's aankoopwaarde / spend      (label: "volgens Meta")
  * blended CAC         = ad spend / nieuwe klanten
  * AOV                 = omzet / aantal orders

Consistency note (why MER uses omzet *excl.* btw): break-even ROAS is
defined as 1/marge-ratio and the marge-ratio is computed on revenue excl.
VAT — so the MER it is compared against must be measured on the same
revenue, or the comparison is off by exactly the VAT rate. Meta's own
ROAS is based on incl-VAT pixel values and is therefore only ever shown
as "volgens Meta", never compared 1-on-1 with break-even ROAS.

Money is integer cents throughout; intermediate math uses Fraction so
rounding happens exactly once per formula, half away from zero (the
common commercial rounding — Python's round() would round half to even).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Sequence

from compass.models import CostModel


# ── rounding ─────────────────────────────────────────────────────────


def _round_half_away(value: Fraction) -> int:
    """Round to the nearest integer, halves away from zero."""
    if value >= 0:
        return int((2 * value + 1) // 2)
    return -int((2 * -value + 1) // 2)


def _frac(rate: float | str) -> Fraction:
    """A float like 0.09 becomes the exact Fraction 9/100 (via str), so
    VAT math is exact instead of inheriting binary-float noise."""
    return Fraction(str(rate))


# ── VAT ──────────────────────────────────────────────────────────────


def excl_vat_cents(gross_cents: int, vat_rate: float) -> int:
    """Omzet excl. btw = omzet incl. btw / (1 + btw-tarief), in cents."""
    return _round_half_away(Fraction(gross_cents) / (1 + _frac(vat_rate)))


def vat_component_cents(gross_cents: int, vat_rate: float) -> int:
    """The VAT inside a gross amount; gross = excl + vat, exactly."""
    return gross_cents - excl_vat_cents(gross_cents, vat_rate)


def pct_of_cents(amount_cents: int, pct: float) -> int:
    """pct (a fraction, 0.124 = 12,4%) of an amount, rounded to cents."""
    return _round_half_away(Fraction(amount_cents) * _frac(pct))


# ── contribution margin per order ────────────────────────────────────


@dataclass(frozen=True)
class MarginBreakdown:
    """The build-up from revenue to contribution margin for one order
    (or one averaged order, in the unit-economics view)."""

    revenue_excl_cents: int
    cogs_cents: int
    shipping_cents: int
    payment_fee_cents: int
    bol_commission_cents: int

    @property
    def margin_cents(self) -> int:
        return (
            self.revenue_excl_cents
            - self.cogs_cents
            - self.shipping_cents
            - self.payment_fee_cents
            - self.bol_commission_cents
        )

    @property
    def ratio(self) -> float | None:
        """Contributiemarge-ratio; None when there is no revenue."""
        if self.revenue_excl_cents <= 0:
            return None
        return self.margin_cents / self.revenue_excl_cents


ZERO_MARGIN = MarginBreakdown(0, 0, 0, 0, 0)


def order_margin(
    *,
    gross_cents: int,
    net_cents: int | None,
    refunded_cents: int,
    status: str,
    units: int,
    channel: str,
    payment_method: str | None,
    cost_model: CostModel,
) -> MarginBreakdown:
    """Contribution margin of one order under a given cost-model version.

    Rules (deliberate, documented in OCHTEND.md as assumptions):
      * A fully refunded order contributes nothing: revenue 0 and costs 0.
        (In reality the shipping and part of the payment fee are sunk;
        we keep refunds simple and honest-to-a-fault instead.)
      * Partial refunds scale revenue down proportionally; unit costs stay
        (the goods were shipped).
      * Payment fees apply to Shopify orders only — on bol, bol collects
        payment and its commission is the cost. Commission and fee
        percentages are taken over the gross (incl. VAT) amount, because
        that is what PSPs and bol actually charge over.
      * When the source gave an exact excl-VAT amount (Shopify does), that
        is used; otherwise (bol, CSV) it is derived via the VAT rate of
        the cost-model version.
    """
    if status == "refunded" or gross_cents <= 0:
        return ZERO_MARGIN

    effective_gross = max(gross_cents - max(refunded_cents, 0), 0)
    if effective_gross == 0:
        return ZERO_MARGIN

    base_net = net_cents if net_cents is not None else excl_vat_cents(
        gross_cents, cost_model.vat_rate
    )
    # Scale the excl-VAT revenue by the refunded share of the gross.
    revenue_excl = _round_half_away(
        Fraction(base_net) * Fraction(effective_gross, gross_cents)
    )

    cogs = cost_model.cogs_per_unit_cents * units
    shipping = cost_model.shipping_per_order_cents

    payment_fee = 0
    bol_commission = 0
    if channel == "bol":
        bol_commission = pct_of_cents(effective_gross, cost_model.bol_commission_pct)
    else:
        fee = cost_model.fee_for(payment_method)
        payment_fee = pct_of_cents(effective_gross, fee.pct) + fee.fixed_cents

    return MarginBreakdown(
        revenue_excl_cents=revenue_excl,
        cogs_cents=cogs,
        shipping_cents=shipping,
        payment_fee_cents=payment_fee,
        bol_commission_cents=bol_commission,
    )


# ── ratios: break-even ROAS, MER, CAC, AOV ───────────────────────────


def margin_ratio(margin_cents: int, revenue_excl_cents: int) -> float | None:
    """Contributiemarge-ratio over any aggregate; None without revenue."""
    if revenue_excl_cents <= 0:
        return None
    return margin_cents / revenue_excl_cents


def break_even_roas(ratio: float | None) -> float | None:
    """1 / contributiemarge-ratio. None when the ratio is missing or ≤ 0
    (a non-positive margin has no break-even: ads can never pay back)."""
    if ratio is None or ratio <= 0:
        return None
    return 1 / ratio


def mer(revenue_excl_cents: int, spend_cents: int) -> float | None:
    """Blended MER = omzet excl. btw / ad spend. None without spend."""
    if spend_cents <= 0:
        return None
    return revenue_excl_cents / spend_cents


def meta_roas(meta_purchase_value_cents: int, spend_cents: int) -> float | None:
    """ROAS as Meta claims it — always label 'volgens Meta'."""
    if spend_cents <= 0:
        return None
    return meta_purchase_value_cents / spend_cents


def blended_cac_cents(spend_cents: int, new_customers: int) -> int | None:
    """Blended CAC = ad spend / new customers, in cents. None without
    new customers (no meaningful acquisition cost that period)."""
    if new_customers <= 0:
        return None
    return _round_half_away(Fraction(spend_cents, new_customers))


def aov_cents(revenue_cents: int, orders_count: int) -> int | None:
    """Average order value over any revenue figure the caller labels."""
    if orders_count <= 0:
        return None
    return _round_half_away(Fraction(revenue_cents, orders_count))


# ── indicative monthly P&L ───────────────────────────────────────────


def prorated_fixed_cents(fixed_month_cents: int, days_covered: int, days_in_month: int) -> int:
    """Fixed monthly costs, prorated for a partial month (the running
    month on the dashboard). Full month → the full amount."""
    if days_in_month <= 0:
        return 0
    days_covered = min(max(days_covered, 0), days_in_month)
    return _round_half_away(
        Fraction(fixed_month_cents) * Fraction(days_covered, days_in_month)
    )


def net_indicative_cents(margin_cents: int, spend_cents: int, fixed_cents: int) -> int:
    """Netto per maand (indicatief) = som contributiemarges − ad spend −
    vaste lasten. Indicative — Compass is not bookkeeping."""
    return margin_cents - spend_cents - fixed_cents


# ── inventory ────────────────────────────────────────────────────────


def weighted_daily_sales(units_by_day: Sequence[int]) -> float:
    """Average units/day over a window, recent days weighing heavier.

    `units_by_day` is ordered oldest → newest (missing days count as 0);
    weights rise linearly 1..n so yesterday counts n× as heavy as the
    oldest day. With the default 30-day window, the most recent week
    carries ~44% of the weight — recent enough to react, damped enough
    not to panic on one quiet day.
    """
    n = len(units_by_day)
    if n == 0:
        return 0.0
    weight_total = n * (n + 1) // 2
    weighted = sum(units * (i + 1) for i, units in enumerate(units_by_day))
    return weighted / weight_total


def days_of_stock(units_on_hand: int, daily_sales: float) -> float | None:
    """How many days the current stock lasts at the given sales rate.
    None when nothing is selling (stock lasts 'forever')."""
    if daily_sales <= 0:
        return None
    return max(units_on_hand, 0) / daily_sales


def reorder_point_units(daily_sales: float, lead_time_days: int, safety_factor: float) -> float:
    """Order new stock when on-hand units drop below this:
    levertijd × verkoopsnelheid × veiligheidsfactor."""
    return max(daily_sales, 0.0) * max(lead_time_days, 0) * max(safety_factor, 0.0)


def reorder_threshold_days(lead_time_days: int, safety_factor: float) -> float:
    """The reorder point expressed in days of stock: levertijd ×
    veiligheidsfactor. One definition for the signal rule, the voorraad
    page and the weekly report, so their verdicts can never disagree."""
    return max(lead_time_days, 0) * max(safety_factor, 0.0)


def sellout_day(on_day: date, days_left: float | None) -> date | None:
    """The expected day stock runs out, whole days (floor: the day the
    last unit sells, not the day after)."""
    if days_left is None:
        return None
    return on_day + timedelta(days=int(days_left))
