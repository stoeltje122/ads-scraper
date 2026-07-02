"""queries: Totals delegation, cost-model versioning, window/month math,
inventory tie-breaking, signal/run accessors and channel economics."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import RUN_DAY

from compass import queries, store
from compass.metrics import MarginBreakdown
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


# ── Totals ───────────────────────────────────────────────────────────


def test_totals_derived_properties_delegate_to_metrics():
    totals = queries.Totals(
        start=DAY1, end=DAY2,
        orders_count=4, units=5,
        revenue_shopify_cents=7985, revenue_bol_cents=2995,
        revenue_excl_cents=10074, margin_cents=5256,
        new_customers=3, spend_cents=7500,
        meta_purchases=2, meta_purchase_value_cents=6000,
    )
    assert totals.revenue_incl_cents == 10980
    assert totals.mer == pytest.approx(10074 / 7500)
    assert totals.meta_roas == pytest.approx(0.8)
    assert totals.cac_cents == 2500
    assert totals.aov_cents == 2745  # 10980 / 4
    assert totals.margin_ratio == pytest.approx(5256 / 10074)
    assert totals.break_even_roas == pytest.approx(10074 / 5256)


def test_totals_without_spend_or_orders_yield_none_ratios():
    totals = queries.Totals(
        start=DAY1, end=DAY1,
        orders_count=0, units=0,
        revenue_shopify_cents=0, revenue_bol_cents=0,
        revenue_excl_cents=0, margin_cents=0,
        new_customers=0, spend_cents=0,
        meta_purchases=0, meta_purchase_value_cents=0,
    )
    assert totals.mer is None
    assert totals.meta_roas is None
    assert totals.cac_cents is None
    assert totals.aov_cents is None
    assert totals.margin_ratio is None
    assert totals.break_even_roas is None


# ── settings & cost model ────────────────────────────────────────────


def test_get_setting_returns_value_or_default(compass_db):
    assert queries.get_setting(compass_db, "signal.streak_days") is None
    assert queries.get_setting(compass_db, "signal.streak_days", "7") == "7"
    store.set_setting(compass_db, "signal.streak_days", "5")
    assert queries.get_setting(compass_db, "signal.streak_days", "7") == "5"


def test_cost_model_for_picks_the_version_valid_on_the_day(compass_db):
    assert queries.cost_model_for(compass_db, RUN_DAY) is None

    store.add_cost_model(compass_db, model())
    store.add_cost_model(
        compass_db, model(valid_from=date(2026, 6, 15), cogs_per_unit_cents=900, note="v2")
    )

    v1 = queries.cost_model_for(compass_db, date(2026, 6, 14))
    assert v1.note == "v1"
    assert v1.cogs_per_unit_cents == 650
    assert v1.id is not None
    assert v1.payment_fees == {"ideal": PaymentFee(pct=0.0, fixed_cents=29)}

    # valid_from itself counts, and pre-history falls back to the earliest.
    assert queries.cost_model_for(compass_db, date(2026, 6, 15)).note == "v2"
    assert queries.cost_model_for(compass_db, date(2026, 1, 1)).note == "v1"


def test_cost_model_history_newest_first(compass_db):
    store.add_cost_model(compass_db, model())
    store.add_cost_model(compass_db, model(valid_from=date(2026, 6, 15), note="v2"))
    assert queries.cost_model_for(compass_db, RUN_DAY).note == "v2"
    assert [m.note for m in queries.cost_model_history(compass_db)] == ["v2", "v1"]


# ── window totals & day rows ─────────────────────────────────────────


def seed_window(conn) -> None:
    """DAY1: two shopify orders (one partially refunded) + one bol order +
    spend; DAY2: one shopify order; DAY3: empty (zero row after rebuild)."""
    store.add_cost_model(conn, model())
    for rec in (
        order("s1", DAY1, hash_="anna"),
        order("s2", DAY1, hash_="bram", refunded=1000),
        order("b1", DAY1, channel="bol", net=None, payment=None),
        order("s3", DAY2, hash_="anna"),
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


def test_window_totals_sums_daily_metrics_over_the_range(compass_db):
    seed_window(compass_db)
    totals = queries.window_totals(compass_db, DAY1, DAY3)

    assert (totals.start, totals.end) == (DAY1, DAY3)
    assert totals.orders_count == 4
    assert totals.units == 4
    # DAY1 shopify 2995 + 1995, DAY2 shopify 2995; bol 2995.
    assert totals.revenue_shopify_cents == 7985
    assert totals.revenue_bol_cents == 2995
    # Excl. VAT: 2748 + 1830 + 2748 + 2748.
    assert totals.revenue_excl_cents == 10074
    # Margins: 1629 + 711 + 1287 + 1629.
    assert totals.margin_cents == 5256
    assert totals.new_customers == 3  # anna + bram + hash-less bol order
    assert totals.spend_cents == 7500
    assert totals.meta_purchases == 2
    assert totals.meta_purchase_value_cents == 6000
    assert totals.mer == pytest.approx(10074 / 7500)
    assert totals.cac_cents == 2500


def test_window_totals_empty_range_is_all_zeros(compass_db):
    totals = queries.window_totals(compass_db, DAY1, DAY3)
    assert totals.orders_count == 0
    assert totals.revenue_incl_cents == 0
    assert totals.mer is None
    assert totals.aov_cents is None


def test_day_rows_ascending_including_the_zero_gap_day(compass_db):
    seed_window(compass_db)
    rows = queries.day_rows(compass_db, DAY1, DAY3)
    assert [row["day"] for row in rows] == ["2026-06-20", "2026-06-21", "2026-06-22"]
    assert rows[2]["orders_count"] == 0


# ── month rows ───────────────────────────────────────────────────────


def test_month_rows_math_with_prorated_fixed_for_the_running_month(compass_db):
    store.add_cost_model(compass_db, model())
    for rec in (
        order("s1", DAY1, hash_="anna"),
        order("s2", DAY1, hash_="bram"),
        order("b1", DAY2, channel="bol", net=None, payment=None),
        order("s3", RUN_DAY, hash_="carla"),  # today, the running month
    ):
        store.upsert_order(compass_db, rec)
    store.upsert_ad_spend(compass_db, spend(DAY1, "c1", spend_cents=5000))
    store.rebuild_daily_metrics(compass_db, date(2026, 6, 1), RUN_DAY)

    months = queries.month_rows(compass_db, today=RUN_DAY)
    assert [m.month for m in months] == ["2026-06", "2026-07"]  # oldest → newest

    june = months[0]
    assert june.totals.orders_count == 3
    assert june.totals.margin_cents == 1629 + 1629 + 1287
    assert june.totals.spend_cents == 5000
    # Cost build-up straight from order_margin sums.
    assert june.cogs_cents == 3 * 650
    assert june.shipping_cents == 3 * 440
    assert june.payment_fee_cents == 2 * 29
    assert june.bol_commission_cents == 371
    assert june.fixed_cents == 40_000  # completed month carries it in full
    assert june.net_cents == 4545 - 5000 - 40_000

    july = months[1]
    assert july.totals.orders_count == 1
    assert (july.totals.start, july.totals.end) == (date(2026, 7, 1), RUN_DAY)
    # 1 of 31 days passed: 40000 × 1/31 = 1290.3 → 1290.
    assert july.fixed_cents == 1290
    assert july.net_cents == 1629 - 0 - 1290


def test_month_rows_skips_empty_months_but_keeps_the_running_month(compass_db):
    assert queries.month_rows(compass_db, today=RUN_DAY) == []

    store.add_cost_model(compass_db, model())
    store.upsert_order(compass_db, order("s1", date(2026, 4, 10), hash_="anna"))
    store.rebuild_daily_metrics(compass_db, date(2026, 4, 10), date(2026, 4, 10))

    months = queries.month_rows(compass_db, today=RUN_DAY)
    # May and June have no data at all; July is the running month.
    assert [m.month for m in months] == ["2026-04", "2026-07"]
    july = months[1]
    assert july.totals.orders_count == 0
    assert july.fixed_cents == 1290
    assert july.net_cents == -1290  # only burning fixed costs so far


# ── units per day & inventory ────────────────────────────────────────


def test_units_sold_by_day_fills_gaps_with_zero(compass_db):
    seed_window(compass_db)
    # One day before and after the rebuilt range: gap-filled with 0.
    units = queries.units_sold_by_day(compass_db, date(2026, 6, 19), date(2026, 6, 23))
    assert units == [0, 3, 1, 0, 0]


def test_latest_inventory_newest_day_wins_then_manual_beats_shopify(compass_db):
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 29), 140, "manual"))
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 30), 120, "shopify"))
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 30), 111, "manual"))
    compass_db.commit()

    latest = queries.latest_inventory(compass_db)
    assert latest == InventoryRecord(day=date(2026, 6, 30), units=111, source="manual")

    # A newer day wins regardless of its (lower-priority) source.
    store.upsert_inventory(compass_db, InventoryRecord(RUN_DAY, 200, "fixture"))
    compass_db.commit()
    assert queries.latest_inventory(compass_db) == InventoryRecord(
        day=RUN_DAY, units=200, source="fixture"
    )


def test_latest_inventory_empty_db_is_none(compass_db):
    assert queries.latest_inventory(compass_db) is None


def test_inventory_series_one_row_per_day_prefers_manual(compass_db):
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 29), 140, "shopify"))
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 30), 120, "shopify"))
    store.upsert_inventory(compass_db, InventoryRecord(date(2026, 6, 30), 111, "manual"))
    compass_db.commit()

    rows = queries.inventory_series(compass_db, date(2026, 6, 28), date(2026, 6, 30))
    assert [(row["day"], row["units"]) for row in rows] == [
        ("2026-06-29", 140),
        ("2026-06-30", 111),
    ]


# ── signals ──────────────────────────────────────────────────────────


def test_signal_accessors_parse_rows_and_order_newest_first(compass_db):
    store.upsert_signal(
        compass_db,
        Signal("scale_up", date(2026, 6, 25), "Overweeg opschalen.", details={"mer": 2.4}),
    )
    store.upsert_signal(
        compass_db,
        Signal("reorder", date(2026, 6, 28), "Bestel voorraad.", explanation="Nog 20 dagen."),
    )
    store.upsert_signal(compass_db, Signal("spend_anomaly", date(2026, 6, 30), "Let op."))
    compass_db.commit()

    active = queries.active_signals(compass_db)
    assert [s.type for s in active] == ["spend_anomaly", "reorder", "scale_up"]
    assert all(s.id is not None for s in active)
    assert active[2].details == {"mer": 2.4}
    assert active[1].explanation == "Nog 20 dagen."

    store.mark_signal_seen(compass_db, active[2].id)
    assert [s.type for s in queries.active_signals(compass_db)] == [
        "spend_anomaly",
        "reorder",
    ]
    # History keeps the seen one, still newest first.
    assert [s.type for s in queries.all_signals(compass_db)] == [
        "spend_anomaly",
        "reorder",
        "scale_up",
    ]
    assert [s.type for s in queries.all_signals(compass_db, limit=1)] == ["spend_anomaly"]

    reorder = queries.signal_by_id(compass_db, active[1].id)
    assert reorder.type == "reorder"
    assert reorder.day == date(2026, 6, 28)
    assert queries.signal_by_id(compass_db, 99_999) is None


# ── runs ─────────────────────────────────────────────────────────────


def _insert_run(conn, started_at: str, ok: int, kind: str = "collect") -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, finished_at, kind, ok) VALUES (?, ?, ?, ?)",
        (started_at, started_at, kind, ok),
    )
    conn.commit()
    return cur.lastrowid


def test_last_runs_and_last_successful_run(compass_db):
    assert queries.last_runs(compass_db) == []
    assert queries.last_successful_run(compass_db) is None

    good = _insert_run(compass_db, "2026-06-29T06:00:00", ok=1)
    bad1 = _insert_run(compass_db, "2026-06-30T06:00:00", ok=0)
    bad2 = _insert_run(compass_db, "2026-07-01T06:00:00", ok=0)

    assert [row["id"] for row in queries.last_runs(compass_db, limit=2)] == [bad2, bad1]
    assert queries.last_successful_run(compass_db)["id"] == good


# ── campaigns ────────────────────────────────────────────────────────


def test_campaign_day_rows_ordered_by_day_then_name(compass_db):
    store.upsert_ad_spend(compass_db, spend(DAY2, "c1"))
    store.upsert_ad_spend(compass_db, spend(DAY1, "c2", campaign_name="Retargeting"))
    store.upsert_ad_spend(compass_db, spend(DAY1, "c1"))
    compass_db.commit()

    rows = queries.campaign_day_rows(compass_db, DAY1, DAY2)
    assert [(row["day"], row["campaign_name"]) for row in rows] == [
        ("2026-06-20", "Prospecting"),
        ("2026-06-20", "Retargeting"),
        ("2026-06-21", "Prospecting"),
    ]


def test_campaign_totals_grouped_sums_biggest_spender_first(compass_db):
    store.upsert_ad_spend(
        compass_db,
        spend(DAY1, "c1", spend_cents=5000, meta_purchases=2, meta_purchase_value_cents=6000),
    )
    store.upsert_ad_spend(
        compass_db,
        spend(DAY2, "c1", spend_cents=4000, meta_purchases=1, meta_purchase_value_cents=2995),
    )
    store.upsert_ad_spend(
        compass_db,
        spend(DAY1, "c2", campaign_name="Retargeting", spend_cents=12_000,
              meta_purchases=None, meta_purchase_value_cents=None),
    )
    compass_db.commit()

    rows = queries.campaign_totals(compass_db, DAY1, DAY2)
    assert [row["campaign_id"] for row in rows] == ["c2", "c1"]

    c1 = rows[1]
    assert c1["campaign_name"] == "Prospecting"
    assert c1["spend_cents"] == 9000
    assert c1["impressions"] == 20_000
    assert c1["clicks"] == 300
    assert c1["meta_purchases"] == 3
    assert c1["meta_purchase_value_cents"] == 8995

    c2 = rows[0]
    assert c2["meta_purchases"] == 0  # NULL claims sum like zero
    assert c2["meta_purchase_value_cents"] == 0


# ── channel economics ────────────────────────────────────────────────


def test_channel_totals_average_shopify_order_rounds_half_away_from_zero(compass_db):
    store.add_cost_model(compass_db, model())
    store.upsert_order(compass_db, order("s1", DAY1, hash_="anna"))
    # Refund of 1002 leaves 1993 gross: excl 2748×1993/2995 = 1828.64 → 1829.
    store.upsert_order(compass_db, order("s2", DAY1, hash_="bram", refunded=1002))
    compass_db.commit()

    econ = queries.channel_totals(compass_db, DAY1, DAY1, "shopify")
    assert econ.channel == "shopify"
    assert econ.orders_count == 2
    assert econ.units == 2
    # Revenue sum 2748 + 1829 = 4577 → avg 2288.5 → half AWAY from zero =
    # 2289 (Python's bankers' round() would say 2288).
    assert econ.avg == MarginBreakdown(2289, 650, 440, 29, 0)
    assert econ.avg.margin_cents == 2289 - 650 - 440 - 29
    # AOV incl. VAT: (2995 + 1993) / 2 = 2494.
    assert econ.aov_incl_cents == 2494


def test_channel_totals_bol_pays_commission_and_empty_channel_is_none(compass_db):
    store.add_cost_model(compass_db, model())
    store.upsert_order(compass_db, order("b1", DAY1, channel="bol", net=None, payment=None))
    # A refunded order never joins the average.
    store.upsert_order(
        compass_db, order("b2", DAY1, channel="bol", net=None, payment=None, status="refunded")
    )
    compass_db.commit()

    bol = queries.channel_totals(compass_db, DAY1, DAY1, "bol")
    assert bol.orders_count == 1
    assert bol.avg == MarginBreakdown(2748, 650, 440, 0, 371)
    assert bol.aov_incl_cents == 2995

    shopify = queries.channel_totals(compass_db, DAY1, DAY1, "shopify")
    assert shopify.orders_count == 0
    assert shopify.units == 0
    assert shopify.avg is None
    assert shopify.aov_incl_cents is None


# ── data presence ────────────────────────────────────────────────────


def test_data_bounds_span_orders_and_spend_days(compass_db):
    assert queries.data_bounds(compass_db) is None

    store.upsert_order(compass_db, order("s1", date(2026, 6, 10)))
    store.upsert_ad_spend(compass_db, spend(date(2026, 6, 5)))
    store.upsert_ad_spend(compass_db, spend(date(2026, 6, 12)))
    compass_db.commit()

    assert queries.data_bounds(compass_db) == (date(2026, 6, 5), date(2026, 6, 12))
