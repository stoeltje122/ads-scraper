"""Read-side queries for dashboard, report, signals and CLI.

Aggregates come from the daily_metrics rollup so pages never scan raw
orders; every derived ratio delegates to compass.metrics so no formula
ever lives twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from compass import metrics
from compass.metrics import MarginBreakdown
from compass.models import CostModel, InventoryRecord, Signal


# ── totals over a day range ──────────────────────────────────────────


@dataclass
class Totals:
    """Sums over a day range from daily_metrics. The ratios are computed
    on the summed cents (not averaged per day) via compass.metrics."""

    start: date
    end: date
    orders_count: int
    units: int
    revenue_shopify_cents: int
    revenue_bol_cents: int
    revenue_excl_cents: int
    margin_cents: int
    new_customers: int
    spend_cents: int
    meta_purchases: int
    meta_purchase_value_cents: int

    @property
    def revenue_incl_cents(self) -> int:
        return self.revenue_shopify_cents + self.revenue_bol_cents

    @property
    def mer(self) -> float | None:
        return metrics.mer(self.revenue_excl_cents, self.spend_cents)

    @property
    def meta_roas(self) -> float | None:
        return metrics.meta_roas(self.meta_purchase_value_cents, self.spend_cents)

    @property
    def cac_cents(self) -> int | None:
        return metrics.blended_cac_cents(self.spend_cents, self.new_customers)

    @property
    def aov_cents(self) -> int | None:
        return metrics.aov_cents(self.revenue_incl_cents, self.orders_count)

    @property
    def margin_ratio(self) -> float | None:
        return metrics.margin_ratio(self.margin_cents, self.revenue_excl_cents)

    @property
    def break_even_roas(self) -> float | None:
        return metrics.break_even_roas(self.margin_ratio)


def window_totals(conn: sqlite3.Connection, start: date, end: date) -> Totals:
    row = conn.execute(
        """SELECT COALESCE(SUM(orders_count), 0)              AS orders_count,
                  COALESCE(SUM(units), 0)                     AS units,
                  COALESCE(SUM(revenue_shopify_cents), 0)     AS revenue_shopify_cents,
                  COALESCE(SUM(revenue_bol_cents), 0)         AS revenue_bol_cents,
                  COALESCE(SUM(revenue_excl_cents), 0)        AS revenue_excl_cents,
                  COALESCE(SUM(margin_cents), 0)              AS margin_cents,
                  COALESCE(SUM(new_customers), 0)             AS new_customers,
                  COALESCE(SUM(spend_cents), 0)               AS spend_cents,
                  COALESCE(SUM(meta_purchases), 0)            AS meta_purchases,
                  COALESCE(SUM(meta_purchase_value_cents), 0) AS meta_purchase_value_cents
           FROM daily_metrics WHERE day BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return Totals(start=start, end=end, **{key: row[key] for key in row.keys()})


def day_rows(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM daily_metrics WHERE day BETWEEN ? AND ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


# ── settings & cost model ────────────────────────────────────────────


def get_setting(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _row_to_cost_model(row: sqlite3.Row) -> CostModel:
    return CostModel(
        valid_from=date.fromisoformat(row["valid_from"]),
        cogs_per_unit_cents=row["cogs_per_unit_cents"],
        shipping_per_order_cents=row["shipping_per_order_cents"],
        fee_pct=row["fee_pct"],
        fee_fixed_cents=row["fee_fixed_cents"],
        payment_fees=CostModel.parse_payment_fees(row["payment_fees_json"]),
        bol_commission_pct=row["bol_commission_pct"],
        vat_rate=row["vat_rate"],
        fixed_month_cents=row["fixed_month_cents"],
        lead_time_days=row["lead_time_days"],
        safety_factor=row["safety_factor"],
        note=row["note"],
        id=row["id"],
    )


def cost_model_for(conn: sqlite3.Connection, day: date) -> CostModel | None:
    """The cost-model version valid on `day`: newest valid_from ≤ day.

    Orders older than the first version fall back to the earliest model —
    a slightly-off margin beats no margin for backfilled history. None
    only when no model exists at all.
    """
    row = conn.execute(
        "SELECT * FROM cost_model WHERE valid_from <= ? ORDER BY valid_from DESC LIMIT 1",
        (day.isoformat(),),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM cost_model ORDER BY valid_from ASC LIMIT 1"
        ).fetchone()
    return _row_to_cost_model(row) if row else None


def cost_model_history(conn: sqlite3.Connection) -> list[CostModel]:
    rows = conn.execute("SELECT * FROM cost_model ORDER BY valid_from DESC").fetchall()
    return [_row_to_cost_model(row) for row in rows]


# ── month summaries (maand view) ─────────────────────────────────────


@dataclass
class MonthSummary:
    """One month for the maand view: totals, the cost build-up behind the
    margin and an *indicative* net result — Compass is not bookkeeping."""

    month: str  # 'YYYY-MM'
    totals: Totals
    cogs_cents: int
    shipping_cents: int
    payment_fee_cents: int
    bol_commission_cents: int
    fixed_cents: int

    @property
    def net_cents(self) -> int:
        return metrics.net_indicative_cents(
            self.totals.margin_cents, self.totals.spend_cents, self.fixed_cents
        )


def _month_bounds(any_day: date) -> tuple[date, date]:
    """First and last calendar day of the month containing `any_day`."""
    start = any_day.replace(day=1)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start - timedelta(days=1)


def _month_has_data(conn: sqlite3.Connection, start: date, end: date) -> bool:
    return (
        conn.execute(
            """SELECT EXISTS (SELECT 1 FROM orders WHERE order_day BETWEEN :a AND :b)
                      OR EXISTS (SELECT 1 FROM ad_spend_daily WHERE day BETWEEN :a AND :b)""",
            {"a": start.isoformat(), "b": end.isoformat()},
        ).fetchone()[0]
        == 1
    )


def _paid_order_sums(
    conn: sqlite3.Connection, start: date, end: date, channel: str | None = None
) -> tuple[dict[str, int], int, int, int]:
    """Sum metrics.order_margin components over the paid orders in a range,
    with the cost-model version of each order's day (cached per day).

    Returns (component sums, orders_count, units, revenue incl. VAT).
    """
    sql = "SELECT * FROM orders WHERE status = 'paid' AND order_day BETWEEN ? AND ?"
    params: list = [start.isoformat(), end.isoformat()]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    orders = conn.execute(sql, params).fetchall()

    sums = {"revenue_excl": 0, "cogs": 0, "shipping": 0, "payment_fee": 0, "bol_commission": 0}
    units = 0
    revenue_incl = 0
    model_cache: dict[str, CostModel | None] = {}
    for o in orders:
        units += o["units"]
        revenue_incl += max(o["gross_cents"] - o["refunded_cents"], 0)
        day_iso = o["order_day"]
        if day_iso not in model_cache:
            model_cache[day_iso] = cost_model_for(conn, date.fromisoformat(day_iso))
        cost_model = model_cache[day_iso]
        if cost_model is None:  # no model at all: costs unknowable, sums stay 0
            continue
        breakdown = metrics.order_margin(
            gross_cents=o["gross_cents"],
            net_cents=o["net_cents"],
            refunded_cents=o["refunded_cents"],
            status=o["status"],
            units=o["units"],
            channel=o["channel"],
            payment_method=o["payment_method"],
            cost_model=cost_model,
        )
        sums["revenue_excl"] += breakdown.revenue_excl_cents
        sums["cogs"] += breakdown.cogs_cents
        sums["shipping"] += breakdown.shipping_cents
        sums["payment_fee"] += breakdown.payment_fee_cents
        sums["bol_commission"] += breakdown.bol_commission_cents
    return sums, len(orders), units, revenue_incl


def _month_summary(
    conn: sqlite3.Connection, m_start: date, m_end: date, today: date
) -> MonthSummary:
    running = m_start <= today <= m_end
    end = today if running else m_end
    totals = window_totals(conn, m_start, end)
    sums, _, _, _ = _paid_order_sums(conn, m_start, end)
    cost_model = cost_model_for(conn, end)
    if cost_model is None:
        fixed = 0
    elif running:
        # The running month only carries the fixed costs of the days that
        # have actually passed — a full month would look artificially red.
        fixed = metrics.prorated_fixed_cents(
            cost_model.fixed_month_cents, today.day, m_end.day
        )
    else:
        fixed = cost_model.fixed_month_cents
    return MonthSummary(
        month=m_start.strftime("%Y-%m"),
        totals=totals,
        cogs_cents=sums["cogs"],
        shipping_cents=sums["shipping"],
        payment_fee_cents=sums["payment_fee"],
        bol_commission_cents=sums["bol_commission"],
        fixed_cents=fixed,
    )


def month_rows(
    conn: sqlite3.Connection, today: date, months: int = 12
) -> list[MonthSummary]:
    """Month summaries oldest→newest. Months without any orders or spend
    are skipped; the running month is always included once the database
    holds any data at all, so day one of the business shows a page."""
    if data_bounds(conn) is None:
        return []
    out: list[MonthSummary] = []
    cursor = today.replace(day=1)
    for i in range(months):
        m_start, m_end = _month_bounds(cursor)
        if i == 0 or _month_has_data(conn, m_start, m_end):
            out.append(_month_summary(conn, m_start, m_end, today))
        cursor = (m_start - timedelta(days=1)).replace(day=1)
    out.reverse()
    return out


# ── inventory & sales rate ───────────────────────────────────────────


def units_sold_by_day(conn: sqlite3.Connection, start: date, end: date) -> list[int]:
    """Units per day oldest→newest, zero-filled for gaps — exactly the
    shape metrics.weighted_daily_sales expects."""
    by_day = {
        row["day"]: row["units"]
        for row in conn.execute(
            "SELECT day, units FROM daily_metrics WHERE day BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
    }
    out: list[int] = []
    day = start
    while day <= end:
        out.append(by_day.get(day.isoformat(), 0))
        day += timedelta(days=1)
    return out


# Same-day snapshots from several sources: the manual recount overrules
# the Shopify number, and fixture data never overrules either.
_SOURCE_PRIORITY_SQL = "CASE source WHEN 'manual' THEN 0 WHEN 'shopify' THEN 1 ELSE 2 END"


def latest_inventory(conn: sqlite3.Connection) -> InventoryRecord | None:
    row = conn.execute(
        f"""SELECT day, units, source FROM inventory_snapshots
            ORDER BY day DESC, {_SOURCE_PRIORITY_SQL} LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    return InventoryRecord(
        day=date.fromisoformat(row["day"]), units=row["units"], source=row["source"]
    )


def inventory_series(
    conn: sqlite3.Connection, start: date, end: date
) -> list[sqlite3.Row]:
    """One (day, units) row per day, preferred source on ties, ascending."""
    return conn.execute(
        f"""SELECT day, units FROM (
                SELECT day, units,
                       ROW_NUMBER() OVER (
                           PARTITION BY day ORDER BY {_SOURCE_PRIORITY_SQL}
                       ) AS pick
                FROM inventory_snapshots WHERE day BETWEEN ? AND ?)
            WHERE pick = 1 ORDER BY day""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


# ── signals ──────────────────────────────────────────────────────────


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        type=row["type"],
        day=date.fromisoformat(row["day"]),
        message=row["message"],
        explanation=row["explanation"] or "",
        details=json.loads(row["details_json"]) if row["details_json"] else {},
        status=row["status"],
        id=row["id"],
    )


def active_signals(conn: sqlite3.Connection) -> list[Signal]:
    rows = conn.execute(
        "SELECT * FROM signals WHERE status = 'new' ORDER BY day DESC, id DESC"
    ).fetchall()
    return [_row_to_signal(row) for row in rows]


def all_signals(conn: sqlite3.Connection, limit: int = 200) -> list[Signal]:
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY day DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_signal(row) for row in rows]


def signal_by_id(conn: sqlite3.Connection, signal_id: int) -> Signal | None:
    row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return _row_to_signal(row) if row else None


# ── runs ─────────────────────────────────────────────────────────────


def last_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_successful_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ── campaigns (advertenties view) ────────────────────────────────────


def campaign_day_rows(
    conn: sqlite3.Connection, start: date, end: date
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM ad_spend_daily WHERE day BETWEEN ? AND ?
           ORDER BY day, campaign_name""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def campaign_totals(
    conn: sqlite3.Connection, start: date, end: date
) -> list[sqlite3.Row]:
    """Per-campaign sums over a window, biggest spender first. NULL
    purchase columns count as 0 — 'Meta claims nothing' sums like zero."""
    return conn.execute(
        """SELECT campaign_id,
                  COALESCE(MAX(campaign_name), campaign_id) AS campaign_name,
                  SUM(spend_cents)                          AS spend_cents,
                  SUM(COALESCE(impressions, 0))             AS impressions,
                  SUM(COALESCE(clicks, 0))                  AS clicks,
                  SUM(COALESCE(meta_purchases, 0))          AS meta_purchases,
                  SUM(COALESCE(meta_purchase_value_cents, 0))
                      AS meta_purchase_value_cents
           FROM ad_spend_daily WHERE day BETWEEN ? AND ?
           GROUP BY campaign_id ORDER BY spend_cents DESC""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


# ── channel unit economics (economie view) ───────────────────────────


@dataclass
class ChannelEconomics:
    """Average paid-order economics of one sales channel over a window."""

    channel: str
    orders_count: int
    units: int
    avg: MarginBreakdown | None
    aov_incl_cents: int | None


def channel_totals(
    conn: sqlite3.Connection, start: date, end: date, channel: str
) -> ChannelEconomics:
    """The AVERAGE order build-up: component sums via metrics.order_margin,
    each divided by orders_count. metrics.aov_cents is exactly that
    division — Fraction math, rounded half away from zero."""
    sums, orders_count, units, revenue_incl = _paid_order_sums(conn, start, end, channel)
    if orders_count == 0:
        return ChannelEconomics(
            channel=channel, orders_count=0, units=0, avg=None, aov_incl_cents=None
        )

    def per_order(total_cents: int) -> int:
        value = metrics.aov_cents(total_cents, orders_count)
        return value if value is not None else 0  # unreachable: orders_count > 0

    avg = MarginBreakdown(
        revenue_excl_cents=per_order(sums["revenue_excl"]),
        cogs_cents=per_order(sums["cogs"]),
        shipping_cents=per_order(sums["shipping"]),
        payment_fee_cents=per_order(sums["payment_fee"]),
        bol_commission_cents=per_order(sums["bol_commission"]),
    )
    return ChannelEconomics(
        channel=channel,
        orders_count=orders_count,
        units=units,
        avg=avg,
        aov_incl_cents=metrics.aov_cents(revenue_incl, orders_count),
    )


# ── data presence ────────────────────────────────────────────────────


def data_bounds(conn: sqlite3.Connection) -> tuple[date, date] | None:
    """(earliest, latest) day with any order or spend row; None when empty."""
    row = conn.execute(
        """SELECT MIN(day) AS lo, MAX(day) AS hi FROM (
               SELECT order_day AS day FROM orders
               UNION ALL
               SELECT day FROM ad_spend_daily)"""
    ).fetchone()
    if row["lo"] is None:
        return None
    return date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])
