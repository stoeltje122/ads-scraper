"""store: upsert idempotency, cost-model versioning, signal status rules,
the runs audit trail and the daily_metrics rebuild (hand-computed cents)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

import pytest

from compass import store
from compass.models import (
    AMSTERDAM,
    AdSpendRecord,
    CostModel,
    InventoryRecord,
    OrderRecord,
    PaymentFee,
    Signal,
)

DAY1 = date(2026, 6, 20)
DAY2 = date(2026, 6, 21)
DAY3 = date(2026, 6, 22)


def model(**overrides) -> CostModel:
    """The reference cost model (same numbers as the metrics tests)."""
    values = dict(
        valid_from=date(2026, 6, 1),
        cogs_per_unit_cents=650,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=0.09,
        fixed_month_cents=40_000,
        note="v1",
    )
    values.update(overrides)
    return CostModel(**values)


def order(
    extern_id: str,
    day: date,
    *,
    channel: str = "shopify",
    gross: int = 2995,
    net: int | None = 2748,
    units: int = 1,
    payment: str | None = "ideal",
    status: str = "paid",
    refunded: int = 0,
    hash_: str | None = None,
) -> OrderRecord:
    return OrderRecord(
        extern_id=extern_id,
        channel=channel,
        ordered_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=AMSTERDAM),
        gross_cents=gross,
        units=units,
        net_cents=net,
        vat_cents=None if net is None else gross - net,
        customer_hash=hash_,
        payment_method=payment,
        status=status,
        refunded_cents=refunded,
        raw={"id": extern_id},
    )


def spend(day: date, campaign: str = "c1", **overrides) -> AdSpendRecord:
    values = dict(
        campaign_name="Prospecting",
        spend_cents=5000,
        impressions=10_000,
        clicks=150,
        meta_purchases=2,
        meta_purchase_value_cents=5990,
        raw={"campaign_id": campaign},
    )
    values.update(overrides)
    return AdSpendRecord(day=day, campaign_id=campaign, **values)


# ── order upserts ────────────────────────────────────────────────────


def test_upsert_order_reports_new_only_once(compass_db):
    assert store.upsert_order(compass_db, order("1001", DAY1)) is True
    assert store.upsert_order(compass_db, order("1001", DAY1)) is False
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_upsert_order_update_refreshes_fields_but_keeps_first_seen(compass_db):
    store.upsert_order(compass_db, order("1001", DAY1, hash_="anna"))
    compass_db.execute("UPDATE orders SET first_seen = '2026-06-20 07:00:00'")
    compass_db.commit()

    # The re-fetched order gained a refund and a different payment method.
    changed = order("1001", DAY1, hash_="anna", refunded=1000, payment="klarna")
    assert store.upsert_order(compass_db, changed) is False

    row = compass_db.execute("SELECT * FROM orders").fetchone()
    assert row["refunded_cents"] == 1000
    assert row["payment_method"] == "klarna"
    assert row["status"] == "paid"
    assert row["first_seen"] == "2026-06-20 07:00:00"

    # A later full refund flips the status on the same row.
    store.upsert_order(compass_db, order("1001", DAY1, hash_="anna", status="refunded"))
    row = compass_db.execute("SELECT * FROM orders").fetchone()
    assert row["status"] == "refunded"
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_upsert_order_same_extern_id_on_both_channels_stays_distinct(compass_db):
    assert store.upsert_order(compass_db, order("42", DAY1, channel="shopify")) is True
    assert store.upsert_order(compass_db, order("42", DAY1, channel="bol", net=None)) is True
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


# ── ad spend upserts ─────────────────────────────────────────────────


def test_upsert_ad_spend_idempotent_and_updates_in_place(compass_db):
    assert store.upsert_ad_spend(compass_db, spend(DAY1)) is True
    assert store.upsert_ad_spend(compass_db, spend(DAY1, spend_cents=5500, clicks=180)) is False

    rows = compass_db.execute("SELECT * FROM ad_spend_daily").fetchall()
    assert len(rows) == 1
    assert rows[0]["spend_cents"] == 5500
    assert rows[0]["clicks"] == 180

    # Same campaign on another day is its own row.
    assert store.upsert_ad_spend(compass_db, spend(DAY2)) is True


# ── inventory ────────────────────────────────────────────────────────


def test_upsert_inventory_latest_units_win_per_day_and_source(compass_db):
    store.upsert_inventory(compass_db, InventoryRecord(DAY1, 120, "shopify"))
    store.upsert_inventory(compass_db, InventoryRecord(DAY1, 95, "shopify"))
    rows = compass_db.execute("SELECT * FROM inventory_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["units"] == 95


def test_set_manual_inventory_keeps_its_own_source_row(compass_db):
    store.upsert_inventory(compass_db, InventoryRecord(DAY1, 120, "shopify"))
    store.set_manual_inventory(compass_db, DAY1, 111)
    by_source = {
        row["source"]: row["units"]
        for row in compass_db.execute("SELECT source, units FROM inventory_snapshots")
    }
    assert by_source == {"shopify": 120, "manual": 111}


# ── cost model & settings ────────────────────────────────────────────


def test_add_cost_model_replaces_the_version_with_same_valid_from(compass_db):
    first_id = store.add_cost_model(compass_db, model(note="eerste"))
    second_id = store.add_cost_model(
        compass_db, model(cogs_per_unit_cents=700, note="typo gefikst")
    )
    assert second_id == first_id
    rows = compass_db.execute("SELECT * FROM cost_model").fetchall()
    assert len(rows) == 1
    assert rows[0]["cogs_per_unit_cents"] == 700
    assert rows[0]["note"] == "typo gefikst"


def test_add_cost_model_new_valid_from_creates_a_new_version(compass_db):
    id_v1 = store.add_cost_model(compass_db, model())
    id_v2 = store.add_cost_model(
        compass_db, model(valid_from=date(2026, 6, 15), cogs_per_unit_cents=900, note="v2")
    )
    assert id_v1 != id_v2
    assert compass_db.execute("SELECT COUNT(*) FROM cost_model").fetchone()[0] == 2
    row = compass_db.execute("SELECT * FROM cost_model WHERE id = ?", (id_v1,)).fetchone()
    assert json.loads(row["payment_fees_json"]) == {"ideal": {"pct": 0.0, "fixed_cents": 29}}


def test_set_setting_inserts_then_overwrites(compass_db):
    store.set_setting(compass_db, "signal.mer_scale_factor", "1.3")
    store.set_setting(compass_db, "signal.mer_scale_factor", "1.4")
    rows = compass_db.execute("SELECT * FROM app_settings").fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == "1.4"


# ── signals ──────────────────────────────────────────────────────────


def test_upsert_signal_refreshes_numbers_but_never_downgrades_seen(compass_db):
    first = Signal(
        type="reorder", day=DAY3, message="Bestel nieuwe voorraad.",
        explanation="Nog 20 dagen.", details={"days_left": 20},
    )
    assert store.upsert_signal(compass_db, first) is True

    signal_id = compass_db.execute("SELECT id FROM signals").fetchone()["id"]
    store.mark_signal_seen(compass_db, signal_id)

    again = Signal(
        type="reorder", day=DAY3, message="Bestel nú voorraad.",
        explanation="Nog 18 dagen.", details={"days_left": 18},
    )
    assert store.upsert_signal(compass_db, again) is False

    row = compass_db.execute("SELECT * FROM signals").fetchone()
    assert row["status"] == "seen"  # the founder's decision stands
    assert row["message"] == "Bestel nú voorraad."
    assert row["explanation"] == "Nog 18 dagen."
    assert json.loads(row["details_json"]) == {"days_left": 18}
    assert compass_db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1


def test_same_signal_type_on_another_day_is_a_new_row(compass_db):
    assert store.upsert_signal(compass_db, Signal("reorder", DAY1, "Bestel.")) is True
    assert store.upsert_signal(compass_db, Signal("reorder", DAY2, "Bestel.")) is True
    assert compass_db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


# ── runs audit trail ─────────────────────────────────────────────────


@dataclass
class FakeSourceResult:
    """Stand-in for collector.SourceResult (built in a later wave)."""

    name: str
    ok: bool = True
    skipped: str | None = None
    fetched: int = 0
    upserted: int = 0
    error: str | None = None


@dataclass
class FakeRunResult:
    """Stand-in matching the RunResult attributes record_run relies on."""

    kind: str = "collect"
    started_at: str = "2026-07-01T05:45:00+00:00"
    finished_at: str = "2026-07-01T05:45:07+00:00"
    sources: list[FakeSourceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    orders_upserted: int = 0
    spend_rows_upserted: int = 0


def test_record_run_writes_the_full_audit_trail(compass_db):
    run = FakeRunResult(
        sources=[
            FakeSourceResult(name="shopify", fetched=12, upserted=3),
            FakeSourceResult(name="meta", skipped="geen credentials (zie README)"),
        ],
        orders_upserted=3,
        spend_rows_upserted=0,
    )
    run_id = store.record_run(compass_db, run)

    row = compass_db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["kind"] == "collect"
    assert row["ok"] == 1
    assert row["started_at"] == "2026-07-01T05:45:00+00:00"
    assert row["orders_upserted"] == 3
    assert row["spend_rows_upserted"] == 0
    assert json.loads(row["errors"]) == []
    detail = json.loads(row["detail"])
    assert [entry["name"] for entry in detail] == ["shopify", "meta"]
    assert detail[0]["upserted"] == 3
    assert detail[1]["skipped"] == "geen credentials (zie README)"


def test_record_run_with_errors_is_marked_not_ok(compass_db):
    run = FakeRunResult(
        kind="backfill",
        sources=[FakeSourceResult(name="bol", ok=False, error="429 na 5 pogingen")],
        errors=["bol: 429 na 5 pogingen"],
        ok=False,
    )
    run_id = store.record_run(compass_db, run)
    row = compass_db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["kind"] == "backfill"
    assert row["ok"] == 0
    assert json.loads(row["errors"]) == ["bol: 429 na 5 pogingen"]


# ── daily metrics rebuild ────────────────────────────────────────────


def seed_reference_days(conn) -> None:
    """Two hand-computable days: mixed channels, a partial and a full
    refund, a hash-less bol order and a repeat customer.

    DAY1: s1 (anna), s2 (bram, €10 partial refund), s3 (carla, fully
    refunded), s4 (bram again, same day), b1 (bol, no customer hash).
    DAY2: s5 (anna's second order). DAY3: nothing.
    """
    store.add_cost_model(conn, model())
    for rec in (
        order("s1", DAY1, hash_="anna"),
        order("s2", DAY1, hash_="bram", refunded=1000),
        order("s3", DAY1, hash_="carla", status="refunded"),
        order("s4", DAY1, hash_="bram"),
        order("b1", DAY1, channel="bol", net=None, payment=None),
        order("s5", DAY2, hash_="anna"),
    ):
        store.upsert_order(conn, rec)
    store.upsert_ad_spend(
        conn, spend(DAY1, "c1", spend_cents=5000, meta_purchases=2, meta_purchase_value_cents=6000)
    )
    store.upsert_ad_spend(
        conn,
        spend(DAY1, "c2", campaign_name="Retargeting", spend_cents=2500,
              meta_purchases=None, meta_purchase_value_cents=None),
    )
    store.rebuild_daily_metrics(conn, DAY1, DAY3)


def _day_row(conn, day: date):
    return conn.execute(
        "SELECT * FROM daily_metrics WHERE day = ?", (day.isoformat(),)
    ).fetchone()


def test_rebuild_daily_metrics_hand_computed_reference_day(compass_db):
    seed_reference_days(compass_db)
    row = _day_row(compass_db, DAY1)

    # Paid orders: s1, s2, s4, b1 — the fully refunded s3 is excluded.
    assert row["orders_count"] == 4
    assert row["units"] == 4
    # Shopify incl. VAT net of refunds: 2995 + (2995−1000) + 2995 = 7985.
    assert row["revenue_shopify_cents"] == 7985
    assert row["revenue_bol_cents"] == 2995
    # Excl. VAT: 2748 + 1830 (partial refund scales) + 2748 + 2748 = 10074.
    assert row["revenue_excl_cents"] == 10074
    # Margins: 1629 + (1830−650−440−29) + 1629 + (2748−650−440−371) = 5256.
    assert row["margin_cents"] == 1629 + 711 + 1629 + 1287
    # New: anna + bram (once, despite two orders) + one hash-less bol order.
    # carla never had a *paid* order, so she is nobody's new customer.
    assert row["new_customers"] == 3
    assert row["spend_cents"] == 7500
    assert row["meta_purchases"] == 2  # NULL purchases on c2 count as 0
    assert row["meta_purchase_value_cents"] == 6000
    assert row["mer"] == pytest.approx(10074 / 7500)
    assert row["cac_cents"] == 2500  # 7500 / 3


def test_rebuild_repeat_customer_not_new_again_and_gap_day_is_zero_row(compass_db):
    seed_reference_days(compass_db)

    day2 = _day_row(compass_db, DAY2)
    assert day2["orders_count"] == 1
    assert day2["new_customers"] == 0  # anna's first paid day was DAY1
    assert day2["margin_cents"] == 1629
    assert day2["spend_cents"] == 0
    assert day2["mer"] is None
    assert day2["cac_cents"] is None

    day3 = _day_row(compass_db, DAY3)
    assert day3 is not None  # every day in range gets a row
    assert day3["orders_count"] == 0
    assert day3["revenue_excl_cents"] == 0
    assert day3["spend_cents"] == 0
    assert day3["mer"] is None


def test_rebuild_uses_the_cost_model_version_valid_on_the_order_day(compass_db):
    store.add_cost_model(compass_db, model())  # cogs 650 from 2026-06-01
    store.add_cost_model(
        compass_db, model(valid_from=date(2026, 6, 15), cogs_per_unit_cents=900, note="v2")
    )
    store.upsert_order(compass_db, order("old", date(2026, 6, 10), hash_="anna"))
    store.upsert_order(compass_db, order("boundary", date(2026, 6, 15), hash_="bram"))
    store.upsert_order(compass_db, order("new", DAY1, hash_="carla"))

    store.rebuild_daily_metrics(compass_db, date(2026, 6, 10), DAY1)

    margins = {
        row["day"]: row["margin_cents"]
        for row in compass_db.execute("SELECT day, margin_cents FROM daily_metrics")
    }
    assert margins["2026-06-10"] == 2748 - 650 - 440 - 29  # v1: 1629
    assert margins["2026-06-15"] == 2748 - 900 - 440 - 29  # v2 from its own valid_from
    assert margins["2026-06-20"] == 2748 - 900 - 440 - 29  # v2: 1379


def test_rebuild_order_before_first_model_falls_back_to_earliest_version(compass_db):
    store.add_cost_model(compass_db, model())  # valid from 2026-06-01
    store.upsert_order(compass_db, order("early", date(2026, 5, 20), hash_="anna"))
    store.rebuild_daily_metrics(compass_db, date(2026, 5, 20), date(2026, 5, 20))
    assert _day_row(compass_db, date(2026, 5, 20))["margin_cents"] == 1629


def test_rebuild_without_any_cost_model_raises_in_dutch(compass_db):
    store.upsert_order(compass_db, order("1", DAY1))
    with pytest.raises(ValueError, match="Geen kostenmodel"):
        store.rebuild_daily_metrics(compass_db, DAY1, DAY1)


def test_rebuild_is_idempotent_and_follows_order_updates(compass_db):
    seed_reference_days(compass_db)
    snapshot = compass_db.execute(
        """SELECT day, orders_count, revenue_excl_cents, margin_cents, new_customers
           FROM daily_metrics ORDER BY day"""
    ).fetchall()

    store.rebuild_daily_metrics(compass_db, DAY1, DAY3)
    again = compass_db.execute(
        """SELECT day, orders_count, revenue_excl_cents, margin_cents, new_customers
           FROM daily_metrics ORDER BY day"""
    ).fetchall()
    assert [tuple(r) for r in again] == [tuple(r) for r in snapshot]

    # s1 is later fully refunded: the next rebuild drops it everywhere, and
    # anna's first *paid* order shifts to DAY2 — she becomes new there.
    store.upsert_order(compass_db, order("s1", DAY1, hash_="anna", status="refunded"))
    store.rebuild_daily_metrics(compass_db, DAY1, DAY3)

    day1 = _day_row(compass_db, DAY1)
    assert day1["orders_count"] == 3
    assert day1["revenue_shopify_cents"] == 7985 - 2995
    assert day1["margin_cents"] == 5256 - 1629
    assert day1["new_customers"] == 2  # bram + the hash-less bol order
    assert _day_row(compass_db, DAY2)["new_customers"] == 1  # anna, now
