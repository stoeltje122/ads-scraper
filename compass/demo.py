"""Deterministic demo data: ~90 days of plausible Cloudplunge history.

Why this module exists: `compass demo` must show every feature of the
dashboard (healthy ad economics, the Meta-vs-actual attribution gap, a
spend anomaly, stock running towards the reorder point) without any
credentials — and through the exact same code path as production.
generate() therefore emits raw API-shaped dicts that pass through the
live parse functions via the fixture sources; nothing is inserted
"pre-parsed".

Everything derives from (today, days, seed) through one random.Random:
two calls with the same arguments yield identical data, so tests can
assert exact outcomes and `compass demo` rebuilds the same database
every time.

The tuned story (defaults, days=90, ending YESTERDAY relative to today):
  * One SKU at €29,95 incl. 9% VAT. Shopify ramps ~6 → ~16 orders/day
    with a weekend dip; bol adds a flat 3–7 orders/day. ~35% repeat
    customers, ~4% refunds, one fully cancelled bol order.
  * Meta spend ramps ~€130 → ~€215/day over three campaigns; Meta's
    attribution claims ~78% of Shopify's actual orders/revenue (the
    honest gap the dashboard shows; retargeting gets extra credit),
    plus one 2.8× spend outlier at today−10 for the charts.
  * MER stays ≥ 1.2 × break-even ROAS (scale-up conditions hold), but
    stock is tuned to ~33 days of weighted sales — below the 30 × 1.3 =
    39-day reorder point — so the reorder signal fires and the inventory
    guard suppresses the scale-up advice: the demo shows the cross-rule.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Sequence

from compass import metrics, store
from compass.models import (
    AMSTERDAM,
    CostModel,
    InventoryRecord,
    PaymentFee,
    ams_today,
)

if TYPE_CHECKING:  # pragma: no cover — collector imports stay lazy at runtime
    from compass import collector

DEMO_SEED = 42

# ── the product ──────────────────────────────────────────────────────
PRICE_CENTS = 2995  # €29,95 incl. 9% VAT — the one SKU
VAT_RATE = 0.09
SKU_TITLE = "Cloudplunge Slaapformule — 60 capsules"
SKU_CODE = "CP-SLEEP-60"
EAN = "8720000000017"

# ── order volume (tuned so all signal conditions genuinely hold) ─────
SHOPIFY_RATE_START = 6.0   # orders/day on the oldest day
SHOPIFY_RATE_END = 16.0    # orders/day yesterday
WEEKEND_FACTOR = 0.8       # Sat/Sun dip
TWO_UNIT_SHARE = 0.10
REPEAT_SHARE = 0.35        # drawn from the pool of earlier customers
FULL_REFUND_SHARE = 0.02
PARTIAL_REFUND_SHARE = 0.02
PARTIAL_REFUND_CENTS = 1000  # ~€10 goodwill refund
BOL_MIN_ORDERS = 3
BOL_MAX_ORDERS = 7
BOL_TWO_UNIT_SHARE = 0.08

# ── Meta spend (cents/day, split over three campaigns) ───────────────
SPEND_START_CENTS = 13_000
SPEND_END_CENTS = 21_500
# (campaign_id, name, spend share, CPM in cents)
CAMPAIGNS = (
    ("23850000000000201", "Prospecting – Advantage+", 0.65, 900),
    ("23850000000000202", "Retargeting – Warm", 0.25, 1350),
    ("23850000000000203", "Brand – Search", 0.10, 650),
)
# Meta claims ~78% of Shopify's actual numbers; retargeting is credited
# far beyond its spend share — the classic attribution skew.
META_CLAIM_FACTOR = 0.78
ATTRIBUTION_SHARES = (0.50, 0.40, 0.10)
ANOMALY_DAYS_AGO = 10      # one prospecting day spends 2.8× normal
ANOMALY_FACTOR = 2.8

# ── inventory (tuned: final stock ≈ 33 days < 39-day reorder point) ──
START_UNITS = 680
RESTOCK_DAYS_AGO = 55
RESTOCK_UNITS = 1500


@dataclass
class DemoData:
    """Raw API-shaped payloads plus the records that skip the API layer."""

    shopify_items: list[dict]
    bol_items: list[dict]
    meta_rows: list[dict]
    inventory: list[InventoryRecord]
    cost_models: list[CostModel]


# ── helpers ──────────────────────────────────────────────────────────


def _api_amount(cents: int) -> str:
    """Cents as the API decimal string ('29.95') the parse edge expects."""
    return f"{cents // 100}.{cents % 100:02d}"


def _moment(day: date, hour: int, minute: int, second: int) -> datetime:
    """An Amsterdam-aware moment; isoformat() carries +02:00/+01:00."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=AMSTERDAM)


def _largest_remainder(total: int, shares: Sequence[float]) -> list[int]:
    """Split an integer by fractional shares without losing units."""
    raw = [total * share for share in shares]
    counts = [int(value) for value in raw]
    order = sorted(range(len(shares)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in range(total - sum(counts)):
        counts[order[i]] += 1
    return counts


def _proportional(total: int, weights: Sequence[int]) -> list[int]:
    """Split an integer proportional to weights; remainder to the heaviest."""
    weight_sum = sum(weights)
    if weight_sum == 0:
        return [0] * len(weights)
    out = [total * weight // weight_sum for weight in weights]
    out[max(range(len(weights)), key=lambda i: weights[i])] += total - sum(out)
    return out


def _demo_cost_models(today: date, days: int) -> list[CostModel]:
    """Two versions with note DEMO: purchasing got cheaper at today−45,
    so the dashboard's version history has something to show."""
    v1 = CostModel(
        valid_from=today - timedelta(days=days),
        cogs_per_unit_cents=700,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=VAT_RATE,
        fixed_month_cents=40_000,
        lead_time_days=30,
        safety_factor=1.3,
        note="DEMO",
    )
    v2 = replace(v1, valid_from=today - timedelta(days=45), cogs_per_unit_cents=650)
    return [v1, v2]


# ── generation ───────────────────────────────────────────────────────


def _shopify_order(
    rng: random.Random,
    day: date,
    order_id: int,
    order_number: int,
    customers: list[dict],
) -> tuple[dict, int, int]:
    """One orders.json-shaped item. Returns (item, paid revenue cents,
    paid units) — zero for a fully refunded order."""
    units = 2 if rng.random() < TWO_UNIT_SHARE else 1
    gross = units * PRICE_CENTS
    vat = metrics.vat_component_cents(gross, VAT_RATE)

    roll = rng.random()
    if roll < 0.70:
        gateway = "iDEAL"
    elif roll < 0.90:
        gateway = "shopify_payments"
    else:
        gateway = "klarna"

    if customers and rng.random() < REPEAT_SHARE:
        customer = rng.choice(customers)
        customer["orders"] += 1
    else:
        customer = {
            "id": 7_100_000_000 + len(customers) + 1,
            "email": f"klant{len(customers) + 1}@demo.invalid",
            "orders": 1,
        }
        customers.append(customer)

    refund_roll = rng.random()
    if refund_roll < FULL_REFUND_SHARE:
        financial_status, refunded = "refunded", gross
    elif refund_roll < FULL_REFUND_SHARE + PARTIAL_REFUND_SHARE:
        financial_status, refunded = "partially_refunded", PARTIAL_REFUND_CENTS
    else:
        financial_status, refunded = "paid", 0

    created = _moment(day, rng.randint(7, 22), rng.randint(0, 59), rng.randint(0, 59))
    refunds = []
    if refunded:
        refunds = [
            {
                "id": 9_300_000_000_000 + order_id,
                "created_at": (created + timedelta(hours=30)).isoformat(),
                "note": "retour ontvangen" if financial_status == "refunded" else "coulance",
                "transactions": [
                    {
                        "id": 9_400_000_000_000 + order_id,
                        "kind": "refund",
                        "status": "success",
                        "amount": _api_amount(refunded),
                        "currency": "EUR",
                    }
                ],
            }
        ]

    current_total = gross - refunded
    item = {
        "id": order_id,
        "order_number": order_number,
        "name": f"#{order_number}",
        "test": False,
        "created_at": created.isoformat(),
        "processed_at": (created + timedelta(seconds=2)).isoformat(),
        "updated_at": (created + timedelta(seconds=2)).isoformat(),
        "cancelled_at": None,
        "currency": "EUR",
        "presentment_currency": "EUR",
        "taxes_included": True,
        "confirmed": True,
        "financial_status": financial_status,
        "fulfillment_status": "fulfilled",
        "total_price": _api_amount(gross),
        "subtotal_price": _api_amount(gross),
        "total_tax": _api_amount(vat),
        "total_discounts": "0.00",
        "current_total_price": _api_amount(current_total),
        "current_total_tax": _api_amount(metrics.vat_component_cents(current_total, VAT_RATE)),
        "payment_gateway_names": [gateway],
        "customer": {
            "id": customer["id"],
            "email": customer["email"],
            "orders_count": customer["orders"],
            "state": "enabled",
        },
        "line_items": [
            {
                "id": order_id * 10 + 1,
                "title": SKU_TITLE,
                "sku": SKU_CODE,
                "quantity": units,
                "price": _api_amount(PRICE_CENTS),
            }
        ],
        "refunds": refunds,
        "email": customer["email"],
    }
    if financial_status == "refunded":
        return item, 0, 0
    return item, gross - refunded, units


def _bol_order(
    rng: random.Random, day: date, order_id: str, cancelled: bool
) -> tuple[dict, int]:
    """One order-detail-shaped bol dict. Returns (detail, paid units)."""
    units = 2 if rng.random() < BOL_TWO_UNIT_SHARE else 1
    placed = _moment(day, rng.randint(8, 22), rng.randint(0, 59), rng.randint(0, 59))
    commission_cents = metrics.pct_of_cents(units * PRICE_CENTS, 0.124)
    detail = {
        "orderId": order_id,
        "pickupPoint": False,
        "orderPlacedDateTime": placed.isoformat(),
        "orderItems": [
            {
                "orderItemId": f"{order_id}-1",
                "cancellationRequest": False,
                "fulfilment": {
                    "method": "FBR",
                    "distributionParty": "RETAILER",
                    "latestDeliveryDate": (day + timedelta(days=2)).isoformat(),
                },
                "offer": {
                    "offerId": "aa1000aa-0000-1111-2222-333344445555",
                    "reference": SKU_CODE,
                },
                "product": {"ean": EAN, "title": SKU_TITLE},
                "quantity": units,
                "quantityShipped": 0 if cancelled else units,
                "quantityCancelled": units if cancelled else 0,
                "unitPrice": PRICE_CENTS / 100,
                "commission": commission_cents / 100,
            }
        ],
    }
    return detail, 0 if cancelled else units


def _meta_day_rows(
    rng: random.Random,
    day: date,
    anomaly_day: date,
    frac: float,
    shopify_orders: int,
    shopify_revenue_cents: int,
) -> list[dict]:
    """Three insights-shaped campaign rows for one day. Meta's claim is a
    tuned ~78% slice of Shopify's ACTUAL numbers — the deliberate gap."""
    spend_total = round(SPEND_START_CENTS + (SPEND_END_CENTS - SPEND_START_CENTS) * frac)
    claim = META_CLAIM_FACTOR + rng.uniform(-0.05, 0.05)
    purchases_total = round(shopify_orders * claim)
    value_total = round(shopify_revenue_cents * claim)
    purchases = _largest_remainder(purchases_total, ATTRIBUTION_SHARES)
    values = _proportional(value_total, purchases)

    rows = []
    for (campaign_id, name, share, cpm_cents), p, v in zip(CAMPAIGNS, purchases, values):
        spend = round(spend_total * share * (1 + rng.uniform(-0.05, 0.05)))
        if day == anomaly_day and campaign_id == CAMPAIGNS[0][0]:
            spend = round(spend * ANOMALY_FACTOR)
        impressions = round(spend * 1000 / (cpm_cents * (1 + rng.uniform(-0.08, 0.08))))
        clicks = max(1, round(impressions * (0.015 + rng.uniform(-0.003, 0.003))))
        actions = [
            {"action_type": "link_click", "value": str(clicks)},
            {"action_type": "landing_page_view", "value": str(round(clicks * 0.72))},
            {"action_type": "add_to_cart", "value": str(round(clicks * 0.2))},
        ]
        action_values = []
        if p > 0:
            # The same conversion under several action types, like the real
            # API; parse_insight_row must pick omni_purchase, not sum them.
            actions.append({"action_type": "omni_purchase", "value": str(p)})
            actions.append({"action_type": "purchase", "value": str(p)})
            action_values.append({"action_type": "omni_purchase", "value": _api_amount(v)})
            action_values.append({"action_type": "purchase", "value": _api_amount(v)})
            if p > 1:
                actions.append(
                    {"action_type": "offsite_conversion.fb_pixel_purchase", "value": str(p - 1)}
                )
                action_values.append(
                    {
                        "action_type": "offsite_conversion.fb_pixel_purchase",
                        "value": _api_amount(v * (p - 1) // p),
                    }
                )
        rows.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": name,
                "spend": _api_amount(spend),
                "impressions": str(impressions),
                "clicks": str(clicks),
                "actions": actions,
                "action_values": action_values,
                "date_start": day.isoformat(),
                "date_stop": day.isoformat(),
            }
        )
    return rows


def generate(today: date, days: int = 90, seed: int = DEMO_SEED) -> DemoData:
    """Build the full demo dataset for the `days` days ending yesterday.

    Deterministic given (today, days, seed): one random.Random drives
    every draw in a fixed order, so equal inputs give equal output.
    """
    rng = random.Random(seed)
    start = today - timedelta(days=days)
    anomaly_day = today - timedelta(days=ANOMALY_DAYS_AGO)
    restock_day = today - timedelta(days=RESTOCK_DAYS_AGO)
    cancelled_bol_day = start + timedelta(days=days // 3)

    shopify_items: list[dict] = []
    bol_items: list[dict] = []
    meta_rows: list[dict] = []
    inventory: list[InventoryRecord] = []

    customers: list[dict] = []  # recurring pool → ~35% repeat orders
    order_id = 6_200_000_000_000
    order_number = 1200
    bol_seq = 0
    stock = START_UNITS

    for i in range(days):
        day = start + timedelta(days=i)
        frac = i / max(days - 1, 1)

        # Shopify: ramping volume with a weekend dip and mild noise.
        rate = SHOPIFY_RATE_START + (SHOPIFY_RATE_END - SHOPIFY_RATE_START) * frac
        if day.weekday() >= 5:
            rate *= WEEKEND_FACTOR
        count = max(1, round(rate + rng.uniform(-1.0, 1.0)))
        day_orders = 0
        day_revenue = 0
        day_units = 0
        for _ in range(count):
            order_id += 1
            order_number += 1
            item, paid_cents, paid_units = _shopify_order(
                rng, day, order_id, order_number, customers
            )
            shopify_items.append(item)
            if paid_units:
                day_orders += 1
                day_revenue += paid_cents
                day_units += paid_units

        # bol: flat-ish volume; exactly one fully cancelled order.
        for j in range(rng.randint(BOL_MIN_ORDERS, BOL_MAX_ORDERS)):
            bol_seq += 1
            detail, paid_units = _bol_order(
                rng,
                day,
                f"255{bol_seq:07d}",
                cancelled=(day == cancelled_bol_day and j == 0),
            )
            bol_items.append(detail)
            day_units += paid_units

        # Meta claims a slice of Shopify's actual numbers for this day.
        meta_rows.extend(
            _meta_day_rows(rng, day, anomaly_day, frac, day_orders, day_revenue)
        )

        # End-of-day stock snapshot; one restock landed at today−55.
        if day == restock_day:
            stock += RESTOCK_UNITS
        stock -= day_units
        inventory.append(InventoryRecord(day=day, units=stock, source="fixture"))

    return DemoData(
        shopify_items=shopify_items,
        bol_items=bol_items,
        meta_rows=meta_rows,
        inventory=inventory,
        cost_models=_demo_cost_models(today, days),
    )


# ── loading ──────────────────────────────────────────────────────────


def load_demo(conn, today: date | None = None, days: int = 90) -> "collector.RunResult":
    """Fill an (empty or existing) Compass database with the demo dataset.

    Takes an open connection and opens nothing itself. Cost models and
    the inventory history are seeded directly (the fixture inventory
    source only reports the newest snapshot; the dashboard's 90-day
    stock chart needs them all); orders and spend go through
    collector.collect with injected fixture sources, so the demo
    exercises the full parse → upsert → rebuild → signals path without
    ever touching the network.
    """
    from compass import collector  # lazy: built on top of this layer
    from compass.sources.fixture import (
        FixtureBolOrders,
        FixtureInventory,
        FixtureMetaSpend,
        FixtureShopifyOrders,
    )

    today = today or ams_today()
    data = generate(today, days=days)

    for cost_model in data.cost_models:
        store.add_cost_model(conn, cost_model)
    for record in data.inventory:
        store.upsert_inventory(conn, record)
    conn.commit()

    return collector.collect(
        conn,
        settings=None,
        since=today - timedelta(days=days),
        until=today,
        today=today,
        kind="demo",
        orders_sources=[
            FixtureShopifyOrders(data.shopify_items),
            FixtureBolOrders(data.bol_items),
        ],
        spend_sources=[FixtureMetaSpend(data.meta_rows)],
        inventory_sources=[FixtureInventory(data.inventory)],
    )
