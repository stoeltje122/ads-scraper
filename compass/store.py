"""Write-side storage: order/spend/inventory upserts, cost-model versions,
signals, the runs audit trail and the daily_metrics rebuild.

Every upsert keys on the natural key of its table, so re-running
collect/backfill/import is always safe. Bulk upserts leave the commit to
the caller (the collector commits once per run); one-shot writes commit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING

from compass import metrics, queries
from compass.models import AdSpendRecord, CostModel, InventoryRecord, OrderRecord, Signal

if TYPE_CHECKING:  # pragma: no cover — the collector is built on top of this module
    from compass import collector

logger = logging.getLogger(__name__)


# ── orders & ad spend ────────────────────────────────────────────────


def upsert_order(conn: sqlite3.Connection, rec: OrderRecord) -> bool:
    """Insert or refresh one order. Returns True when the order is new.

    On conflict every data field is refreshed — a re-fetched order may
    have gained a refund or a status change — but first_seen is kept:
    it records when *we* first saw the order, not when the source did.
    """
    existing = conn.execute(
        "SELECT id FROM orders WHERE channel = ? AND extern_id = ?",
        (rec.channel, rec.extern_id),
    ).fetchone()
    conn.execute(
        """INSERT INTO orders (extern_id, channel, order_day, ordered_at, gross_cents,
                               net_cents, vat_cents, units, customer_hash,
                               is_new_customer, payment_method, status,
                               refunded_cents, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(channel, extern_id) DO UPDATE SET
             order_day = excluded.order_day,
             ordered_at = excluded.ordered_at,
             gross_cents = excluded.gross_cents,
             net_cents = excluded.net_cents,
             vat_cents = excluded.vat_cents,
             units = excluded.units,
             customer_hash = excluded.customer_hash,
             is_new_customer = excluded.is_new_customer,
             payment_method = excluded.payment_method,
             status = excluded.status,
             refunded_cents = excluded.refunded_cents,
             raw_json = excluded.raw_json,
             updated_at = datetime('now')""",
        (
            rec.extern_id,
            rec.channel,
            rec.order_day.isoformat(),
            rec.ordered_at.isoformat(),
            rec.gross_cents,
            rec.net_cents,
            rec.vat_cents,
            rec.units,
            rec.customer_hash,
            None if rec.is_new_customer is None else int(rec.is_new_customer),
            rec.payment_method,
            rec.status,
            rec.refunded_cents,
            rec.raw_json(),
        ),
    )
    return existing is None


def upsert_ad_spend(conn: sqlite3.Connection, rec: AdSpendRecord) -> bool:
    """Insert or refresh one campaign-day. Returns True when it is new."""
    existing = conn.execute(
        "SELECT id FROM ad_spend_daily WHERE day = ? AND campaign_id = ?",
        (rec.day.isoformat(), rec.campaign_id),
    ).fetchone()
    conn.execute(
        """INSERT INTO ad_spend_daily (day, campaign_id, campaign_name, spend_cents,
                                       impressions, clicks, meta_purchases,
                                       meta_purchase_value_cents, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(day, campaign_id) DO UPDATE SET
             campaign_name = excluded.campaign_name,
             spend_cents = excluded.spend_cents,
             impressions = excluded.impressions,
             clicks = excluded.clicks,
             meta_purchases = excluded.meta_purchases,
             meta_purchase_value_cents = excluded.meta_purchase_value_cents,
             raw_json = excluded.raw_json,
             updated_at = datetime('now')""",
        (
            rec.day.isoformat(),
            rec.campaign_id,
            rec.campaign_name,
            rec.spend_cents,
            rec.impressions,
            rec.clicks,
            rec.meta_purchases,
            rec.meta_purchase_value_cents,
            rec.raw_json(),
        ),
    )
    return existing is None


# ── inventory ────────────────────────────────────────────────────────


def upsert_inventory(conn: sqlite3.Connection, rec: InventoryRecord) -> bool:
    """Latest count wins per (day, source): a later fetch on the same day
    is fresher truth, not a duplicate. Returns True when the row is new,
    like the other upserts, so import counts stay honest."""
    existing = conn.execute(
        "SELECT 1 FROM inventory_snapshots WHERE day = ? AND source = ?",
        (rec.day.isoformat(), rec.source),
    ).fetchone()
    conn.execute(
        """INSERT INTO inventory_snapshots (day, units, source)
           VALUES (?, ?, ?)
           ON CONFLICT(day, source) DO UPDATE SET units = excluded.units""",
        (rec.day.isoformat(), rec.units, rec.source),
    )
    return existing is None


def set_manual_inventory(conn: sqlite3.Connection, day: date, units: int) -> None:
    """Manual stock count from the dashboard. Stored as its own source:
    the read side prefers 'manual' on a same-day tie (the human recount
    is the correction, not the Shopify number)."""
    upsert_inventory(conn, InventoryRecord(day=day, units=units, source="manual"))
    conn.commit()


# ── cost model & settings ────────────────────────────────────────────


def add_cost_model(conn: sqlite3.Connection, cm: CostModel) -> int:
    """Add a cost-model version; returns its row id.

    A second submit for the same valid_from replaces that version —
    fixing a typo must never create a shadow version for the same day.
    """
    conn.execute(
        """INSERT INTO cost_model (valid_from, cogs_per_unit_cents,
               shipping_per_order_cents, fee_pct, fee_fixed_cents,
               payment_fees_json, bol_commission_pct, vat_rate,
               fixed_month_cents, lead_time_days, safety_factor, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(valid_from) DO UPDATE SET
             cogs_per_unit_cents = excluded.cogs_per_unit_cents,
             shipping_per_order_cents = excluded.shipping_per_order_cents,
             fee_pct = excluded.fee_pct,
             fee_fixed_cents = excluded.fee_fixed_cents,
             payment_fees_json = excluded.payment_fees_json,
             bol_commission_pct = excluded.bol_commission_pct,
             vat_rate = excluded.vat_rate,
             fixed_month_cents = excluded.fixed_month_cents,
             lead_time_days = excluded.lead_time_days,
             safety_factor = excluded.safety_factor,
             note = excluded.note""",
        (
            cm.valid_from.isoformat(),
            cm.cogs_per_unit_cents,
            cm.shipping_per_order_cents,
            cm.fee_pct,
            cm.fee_fixed_cents,
            cm.payment_fees_json(),
            cm.bol_commission_pct,
            cm.vat_rate,
            cm.fixed_month_cents,
            cm.lead_time_days,
            cm.safety_factor,
            cm.note,
        ),
    )
    conn.commit()
    # lastrowid is unreliable on the conflict path — select by natural key.
    row = conn.execute(
        "SELECT id FROM cost_model WHERE valid_from = ?", (cm.valid_from.isoformat(),)
    ).fetchone()
    return row["id"]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value, updated_at = datetime('now')""",
        (key, value),
    )
    conn.commit()


# ── signals ──────────────────────────────────────────────────────────


def upsert_signal(conn: sqlite3.Connection, sig: Signal) -> bool:
    """Insert a signal, or refresh its numbers. Returns True when new.

    The status is never touched on conflict: once a founder marked a
    signal 'seen', a re-evaluation of the same rule on the same day must
    not flip it back to 'new'.
    """
    existing = conn.execute(
        "SELECT id FROM signals WHERE type = ? AND day = ?",
        (sig.type, sig.day.isoformat()),
    ).fetchone()
    conn.execute(
        """INSERT INTO signals (type, day, message, explanation, status, details_json)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(type, day) DO UPDATE SET
             message = excluded.message,
             explanation = excluded.explanation,
             details_json = excluded.details_json""",
        (
            sig.type,
            sig.day.isoformat(),
            sig.message,
            sig.explanation,
            sig.status,
            json.dumps(sig.details, ensure_ascii=False),
        ),
    )
    return existing is None


def mark_signal_seen(conn: sqlite3.Connection, signal_id: int) -> None:
    conn.execute("UPDATE signals SET status = 'seen' WHERE id = ?", (signal_id,))
    conn.commit()


# ── runs audit trail ─────────────────────────────────────────────────


def record_run(conn: sqlite3.Connection, run: "collector.RunResult") -> int:
    """Append one run to the audit trail (also on failure: `compass
    status` must show what happened, not only the log file)."""
    detail = [
        {
            "name": s.name,
            "ok": s.ok,
            "skipped": s.skipped,
            "fetched": s.fetched,
            "upserted": s.upserted,
            "error": s.error,
        }
        for s in run.sources
    ]
    cur = conn.execute(
        """INSERT INTO runs (started_at, finished_at, kind, ok, orders_upserted,
                             spend_rows_upserted, errors, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.started_at,
            run.finished_at,
            run.kind,
            1 if run.ok else 0,
            run.orders_upserted,
            run.spend_rows_upserted,
            json.dumps(run.errors, ensure_ascii=False),
            json.dumps(detail, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid


# ── daily metrics rollup ─────────────────────────────────────────────


def rebuild_daily_metrics(conn: sqlite3.Connection, start: date, end: date) -> None:
    """Rebuild the materialized per-day rollup for [start, end], inclusive.

    Derived data only — safe to rebuild any time. Margins use the
    cost-model version valid on each order's day (queries.cost_model_for),
    so editing the model retroactively corrects history. A row is written
    for every day in the range; the dashboard never has to guess whether
    a missing day means "no sales" or "not built yet".
    """
    if queries.cost_model_for(conn, start) is None:
        raise ValueError(
            "Geen kostenmodel — vul eerst het kostenmodel in "
            "(`compass init` of het dashboard)."
        )

    # Earliest *paid* day per customer_hash over all time: a customer is
    # new on the day of their first paid order, and only on that day.
    new_customer_days: dict[str, int] = {
        row["first_day"]: row["n"]
        for row in conn.execute(
            """SELECT first_day, COUNT(*) AS n FROM (
                   SELECT customer_hash, MIN(order_day) AS first_day
                   FROM orders WHERE status = 'paid' AND customer_hash IS NOT NULL
                   GROUP BY customer_hash)
               GROUP BY first_day"""
        )
    }

    # The delete + per-day inserts below commit together or roll back
    # together: a crash halfway must never leave a hole in the rollup.
    # Callers therefore commit their own pending writes BEFORE calling
    # (the collector and importer do), or lose them with the rollback.
    try:
        _rebuild_range(conn, start, end, new_customer_days)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    logger.info("Dagcijfers herbouwd: %s t/m %s", start.isoformat(), end.isoformat())


def _rebuild_range(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    new_customer_days: dict[str, int],
) -> None:
    conn.execute(
        "DELETE FROM daily_metrics WHERE day BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )

    # The cost-model table holds a handful of versions: load them once and
    # pick per day, instead of one SQL query + JSON parse per day.
    versions = sorted(queries.cost_model_history(conn), key=lambda m: m.valid_from)

    def model_for(target: date) -> CostModel:
        chosen = versions[0]
        for version in versions:
            if version.valid_from <= target:
                chosen = version
            else:
                break
        return chosen

    day = start
    while day <= end:
        iso = day.isoformat()
        cost_model = model_for(day)

        orders = conn.execute(
            "SELECT * FROM orders WHERE order_day = ? AND status = 'paid'", (iso,)
        ).fetchall()
        units = 0
        revenue_shopify = revenue_bol = 0
        revenue_excl = margin = 0
        for o in orders:
            units += o["units"]
            net_of_refunds = max(o["gross_cents"] - o["refunded_cents"], 0)
            if o["channel"] == "bol":
                revenue_bol += net_of_refunds
            else:
                revenue_shopify += net_of_refunds
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
            revenue_excl += breakdown.revenue_excl_cents
            margin += breakdown.margin_cents

        # Hash-less paid orders count as new customers by spec: bol masks
        # buyer identity, and undercounting acquisition inflates CAC quality.
        hashless = conn.execute(
            """SELECT COUNT(*) FROM orders
               WHERE status = 'paid' AND customer_hash IS NULL AND order_day = ?""",
            (iso,),
        ).fetchone()[0]
        new_customers = new_customer_days.get(iso, 0) + hashless

        spend = conn.execute(
            """SELECT COALESCE(SUM(spend_cents), 0) AS spend_cents,
                      COALESCE(SUM(COALESCE(meta_purchases, 0)), 0) AS meta_purchases,
                      COALESCE(SUM(COALESCE(meta_purchase_value_cents, 0)), 0)
                          AS meta_purchase_value_cents
               FROM ad_spend_daily WHERE day = ?""",
            (iso,),
        ).fetchone()

        conn.execute(
            """INSERT INTO daily_metrics (day, orders_count, units,
                   revenue_shopify_cents, revenue_bol_cents, revenue_excl_cents,
                   margin_cents, new_customers, spend_cents, meta_purchases,
                   meta_purchase_value_cents, mer, cac_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                iso,
                len(orders),
                units,
                revenue_shopify,
                revenue_bol,
                revenue_excl,
                margin,
                new_customers,
                spend["spend_cents"],
                spend["meta_purchases"],
                spend["meta_purchase_value_cents"],
                metrics.mer(revenue_excl, spend["spend_cents"]),
                metrics.blended_cac_cents(spend["spend_cents"], new_customers),
            ),
        )
        day += timedelta(days=1)
