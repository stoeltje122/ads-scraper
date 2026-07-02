"""signals: threshold defaults/overrides, the four rules firing and staying
quiet, the inventory suppression cross-rule, cooldown and degraded modes
(missing cost model, empty database)."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import RUN_DAY

from compass import queries, signals, store
from compass.models import (
    AdSpendRecord,
    CostModel,
    InventoryRecord,
    PaymentFee,
    Signal,
)

YESTERDAY = RUN_DAY - timedelta(days=1)  # 2026-06-30, the last complete day


def add_model(conn, **overrides) -> None:
    """The reference cost model: break-even maths as in the queries tests,
    reorder point 30 × 1.3 = 39 days."""
    values = dict(
        valid_from=date(2026, 1, 1),
        cogs_per_unit_cents=650,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=0.09,
        fixed_month_cents=40_000,
        lead_time_days=30,
        safety_factor=1.3,
        note="test",
    )
    values.update(overrides)
    store.add_cost_model(conn, CostModel(**values))


def put_day(
    conn,
    day: date,
    *,
    revenue_excl: int = 0,
    margin: int = 0,
    spend: int = 0,
    units: int = 0,
    new_customers: int = 0,
    orders: int = 0,
) -> None:
    """Write one daily_metrics row directly — signals only read the rollup."""
    conn.execute(
        """INSERT OR REPLACE INTO daily_metrics
               (day, orders_count, units, revenue_shopify_cents,
                revenue_bol_cents, revenue_excl_cents, margin_cents,
                new_customers, spend_cents, meta_purchases,
                meta_purchase_value_cents)
           VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, 0, 0)""",
        (day.isoformat(), orders, units, revenue_excl, margin, new_customers, spend),
    )


def seed_days(
    conn,
    *,
    days: int = 20,
    revenue_excl: int = 30_000,
    margin: int = 15_000,
    spend: int = 5_000,
    units: int = 10,
    new_customers: int = 2,
) -> None:
    """`days` identical complete days ending yesterday. With the defaults
    every 7-day window has MER 6,00 vs break-even ROAS 2,00 and a stable
    CAC of €25 — comfortably scale-up worthy."""
    for offset in range(1, days + 1):
        put_day(
            conn,
            RUN_DAY - timedelta(days=offset),
            revenue_excl=revenue_excl,
            margin=margin,
            spend=spend,
            units=units,
            new_customers=new_customers,
            orders=max(units, 1),
        )
    conn.commit()


def set_stock(conn, units: int, day: date = YESTERDAY, source: str = "manual") -> None:
    store.upsert_inventory(conn, InventoryRecord(day=day, units=units, source=source))
    conn.commit()


def signal_types(conn) -> list[str]:
    return [row["type"] for row in conn.execute("SELECT type FROM signals ORDER BY id")]


# ── thresholds & labels ──────────────────────────────────────────────


def test_get_thresholds_defaults_and_settings_overrides(compass_db):
    assert signals.get_thresholds(compass_db) == signals.THRESHOLD_DEFAULTS

    store.set_setting(compass_db, "signal.mer_scale_factor", "1,4")  # Dutch comma
    store.set_setting(compass_db, "signal.streak_days", "3")
    store.set_setting(compass_db, "signal.min_spend_cents", "5000")

    thresholds = signals.get_thresholds(compass_db)
    assert thresholds["mer_scale_factor"] == 1.4
    assert thresholds["streak_days"] == 3
    assert thresholds["min_spend_cents"] == 5000
    assert thresholds["cac_tolerance"] == 1.05  # untouched keys keep defaults


def test_get_thresholds_falls_back_on_unparseable_override(compass_db):
    store.set_setting(compass_db, "signal.cac_tolerance", "veel te veel")
    assert signals.get_thresholds(compass_db)["cac_tolerance"] == 1.05


def test_signal_type_label_maps_to_dutch():
    assert signals.signal_type_label("scale_up") == "Opschalen"
    assert signals.signal_type_label("scale_down") == "Afschalen"
    assert signals.signal_type_label("reorder") == "Voorraad bestellen"
    assert signals.signal_type_label("spend_anomaly") == "Spend-afwijking"
    assert signals.signal_type_label("onbekend") == "onbekend"  # safe fallback


# ── scale_up ─────────────────────────────────────────────────────────


def test_scale_up_fires_on_healthy_streak_with_stable_cac(compass_db):
    add_model(compass_db)
    seed_days(compass_db)        # MER 6,00 vs break-even 2,00, CAC €25 both weeks
    set_stock(compass_db, 1000)  # ±113 days of stock: the guard stays quiet

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["scale_up"]
    signal = fired[0]
    assert signal.day == RUN_DAY
    assert signal.status == "new"
    assert signal.message == (
        "Overweeg het advertentiebudget stapsgewijs te verhogen (bijv. +20%)."
    )
    assert "MER 6,00" in signal.explanation
    assert "break-even 2,00" in signal.explanation
    assert "€ 25,00" in signal.explanation  # CAC now and a week earlier
    assert signal.details["cac_now_cents"] == 2500
    assert signal.details["cac_prev_cents"] == 2500
    assert signal.details["streak_days"] == 7

    # Persisted and committed (evaluate commits its own upserts).
    assert [s.type for s in queries.active_signals(compass_db)] == ["scale_up"]
    assert not compass_db.in_transaction


def test_scale_up_stays_quiet_when_one_streak_day_breaks(compass_db):
    add_model(compass_db)
    seed_days(compass_db)
    set_stock(compass_db, 1000)
    # One heavy-loss day: windows containing it get break-even ROAS 7,00,
    # far above MER 6,00 × … — the streak is broken.
    put_day(
        compass_db, RUN_DAY - timedelta(days=3),
        revenue_excl=30_000, margin=-60_000, spend=5_000, units=10, new_customers=2,
    )
    compass_db.commit()

    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []


def test_scale_up_requires_a_stable_cac_week_over_week(compass_db):
    add_model(compass_db)
    seed_days(compass_db, new_customers=4)
    # This week half the new customers: CAC €25,00 vs €12,50 → not stable.
    for offset in range(1, 8):
        put_day(
            compass_db, RUN_DAY - timedelta(days=offset),
            revenue_excl=30_000, margin=15_000, spend=5_000, units=10,
            new_customers=2, orders=10,
        )
    compass_db.commit()

    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []


def test_mer_rules_ignore_weeks_with_too_little_spend(compass_db):
    add_model(compass_db)
    # MER 15 (far above break-even) but only €20/day: below the €25 floor,
    # so neither scale_up nor scale_down may conclude anything.
    seed_days(compass_db, spend=2_000)

    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []


# ── scale_down ───────────────────────────────────────────────────────


def test_scale_down_fires_below_break_even_and_names_worst_campaign(compass_db):
    add_model(compass_db)
    # MER 1,60 vs break-even 2,00, every day of the streak.
    seed_days(compass_db, revenue_excl=8_000, margin=4_000, units=3, new_customers=1)
    day = RUN_DAY - timedelta(days=5)
    for campaign_id, name, spend, value in (
        ("c1", "Prospecting", 3_000, 1_500),  # ROAS 0,50 volgens Meta → worst
        ("c2", "Retargeting", 3_000, 9_000),  # ROAS 3,00 volgens Meta
        ("c3", "Klein", 1_000, 0),            # below min spend: never named
    ):
        store.upsert_ad_spend(
            compass_db,
            AdSpendRecord(
                day=day, campaign_id=campaign_id, campaign_name=name,
                spend_cents=spend, meta_purchases=1,
                meta_purchase_value_cents=value,
            ),
        )
    compass_db.commit()

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["scale_down"]
    signal = fired[0]
    assert signal.day == RUN_DAY
    assert signal.message == (
        "Je verliest geld op advertenties; overweeg het budget te verlagen "
        "of de slechtste campagne te pauzeren."
    )
    assert "MER 1,60" in signal.explanation
    assert "break-even 2,00" in signal.explanation
    assert "'Prospecting'" in signal.explanation
    assert "0,50 volgens Meta" in signal.explanation
    assert signal.details["worst_campaign"]["name"] == "Prospecting"


def test_scale_down_without_campaign_data_keeps_generic_explanation(compass_db):
    add_model(compass_db)
    seed_days(compass_db, revenue_excl=8_000, margin=4_000, units=3, new_customers=1)

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["scale_down"]
    assert "Slechtste campagne" not in fired[0].explanation
    assert fired[0].details["worst_campaign"] is None


def test_scale_down_needs_the_full_streak_below_break_even(compass_db):
    add_model(compass_db)
    seed_days(compass_db, revenue_excl=8_000, margin=4_000, units=3, new_customers=1)
    # One strong day lifts the windows that contain it above break-even.
    put_day(
        compass_db, RUN_DAY - timedelta(days=4),
        revenue_excl=30_000, margin=15_000, spend=5_000, units=10, new_customers=1,
    )
    compass_db.commit()

    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []


# ── reorder ──────────────────────────────────────────────────────────


def test_reorder_fires_when_stock_dips_below_the_reorder_point(compass_db):
    add_model(compass_db)  # reorder point: 30 × 1,3 = 39 days
    # 10 units/day, constant → weighted rate exactly 10,0.
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    set_stock(compass_db, 200, source="shopify")  # 20 days left

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["reorder"]
    signal = fired[0]
    assert signal.day == RUN_DAY
    assert signal.message == (
        "Bestel nieuwe voorraad; bij de huidige verkoopsnelheid ben je "
        "over 20 dagen uitverkocht (rond 21-07-2026)."
    )
    assert "200 stuks" in signal.explanation
    assert "10,0 per dag" in signal.explanation
    assert "39 dagen" in signal.explanation
    assert "veiligheidsfactor 1,3" in signal.explanation
    assert signals.SUPPRESSED_SCALE_UP_LINE not in signal.explanation
    assert signal.details["days_left"] == 20.0
    assert signal.details["sellout_day"] == "2026-07-21"
    assert signal.details["scale_up_suppressed"] is False


def test_reorder_stays_quiet_with_ample_stock_or_no_sales(compass_db):
    add_model(compass_db)
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    set_stock(compass_db, 600)  # 60 days ≥ the 39-day reorder point
    assert signals.evaluate(compass_db, RUN_DAY) == []

    # Nothing selling → stock lasts "forever" → no reorder either.
    compass_db.execute("UPDATE daily_metrics SET units = 0")
    compass_db.commit()
    set_stock(compass_db, 5)
    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []


def test_reorder_without_any_inventory_snapshot_stays_quiet(compass_db):
    add_model(compass_db)
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    assert signals.evaluate(compass_db, RUN_DAY) == []


# ── the suppression cross-rule ───────────────────────────────────────


def test_low_stock_suppresses_scale_up_and_annotates_reorder(compass_db):
    add_model(compass_db)
    seed_days(compass_db)       # scale-up worthy: MER 6,00, CAC stable
    set_stock(compass_db, 100)  # ±11 days left, far below the 39-day point

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["reorder"]
    reorder = fired[0]
    assert signals.SUPPRESSED_SCALE_UP_LINE in reorder.explanation
    assert reorder.details["scale_up_suppressed"] is True
    # The scale-up signal itself must NOT exist anywhere.
    assert signal_types(compass_db) == ["reorder"]


# ── cooldown ─────────────────────────────────────────────────────────


def test_cooldown_blocks_a_recent_same_type_signal_even_when_seen(compass_db):
    add_model(compass_db)
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    set_stock(compass_db, 200)
    store.upsert_signal(
        compass_db,
        Signal("reorder", RUN_DAY - timedelta(days=3), "Bestel voorraad.", status="seen"),
    )
    compass_db.commit()

    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == ["reorder"]  # only the planted one


def test_cooldown_expires_after_signal_cooldown_days(compass_db):
    add_model(compass_db)
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    set_stock(compass_db, 200)
    # Exactly cooldown days old: day > today − 7 is false → no longer blocks.
    store.upsert_signal(
        compass_db, Signal("reorder", RUN_DAY - timedelta(days=7), "Bestel voorraad.")
    )
    compass_db.commit()

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["reorder"]
    assert signal_types(compass_db) == ["reorder", "reorder"]


def test_reevaluation_on_the_same_day_reports_nothing_new(compass_db):
    add_model(compass_db)
    seed_days(compass_db)
    set_stock(compass_db, 1000)

    assert [s.type for s in signals.evaluate(compass_db, RUN_DAY)] == ["scale_up"]
    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == ["scale_up"]


def test_cooldown_days_threshold_override(compass_db):
    add_model(compass_db)
    seed_days(compass_db, days=30, revenue_excl=0, margin=0, spend=0, units=10)
    set_stock(compass_db, 200)
    store.upsert_signal(
        compass_db, Signal("reorder", RUN_DAY - timedelta(days=3), "Bestel voorraad.")
    )
    compass_db.commit()

    assert signals.evaluate(compass_db, RUN_DAY) == []  # default 7 days blocks

    store.set_setting(compass_db, "signal.signal_cooldown_days", "2")
    assert [s.type for s in signals.evaluate(compass_db, RUN_DAY)] == ["reorder"]


# ── spend_anomaly ────────────────────────────────────────────────────


def test_spend_anomaly_fires_on_yesterdays_spike(compass_db):
    # No cost model at all: the anomaly rule must work regardless.
    for offset in range(2, 9):  # the 7 days before yesterday: steady €40/day
        put_day(compass_db, RUN_DAY - timedelta(days=offset), spend=4_000)
    put_day(compass_db, YESTERDAY, spend=12_000)  # 3× the trailing average
    compass_db.commit()

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["spend_anomaly"]
    signal = fired[0]
    assert signal.day == YESTERDAY  # the anomaly signal carries the anomalous day
    assert signal.message == (
        "Let op: de spend van gisteren (€ 120,00) is meer dan 2× het "
        "7-daagse gemiddelde (€ 40,00)."
    )
    assert "3,0× zo veel" in signal.explanation
    assert signal.details["spend_cents"] == 12_000

    # Same day again: the row is refreshed, not re-reported (UNIQUE type+day).
    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == ["spend_anomaly"]


def test_spend_anomaly_needs_history_a_spike_and_minimum_spend(compass_db):
    # Below the anomaly factor: 7000 is not > 2 × 4000.
    for offset in range(2, 9):
        put_day(compass_db, RUN_DAY - timedelta(days=offset), spend=4_000)
    put_day(compass_db, YESTERDAY, spend=7_000)
    compass_db.commit()
    assert signals.evaluate(compass_db, RUN_DAY) == []

    # A 4× relative spike below the €25 absolute floor stays quiet too.
    compass_db.execute("UPDATE daily_metrics SET spend_cents = 500")
    put_day(compass_db, YESTERDAY, spend=2_000)
    compass_db.commit()
    assert signals.evaluate(compass_db, RUN_DAY) == []

    # No history at all → nothing to compare against → skip.
    compass_db.execute(
        "DELETE FROM daily_metrics WHERE day != ?", (YESTERDAY.isoformat(),)
    )
    put_day(compass_db, YESTERDAY, spend=50_000)
    compass_db.commit()
    assert signals.evaluate(compass_db, RUN_DAY) == []

    assert signal_types(compass_db) == []


def test_spend_anomaly_factor_threshold_override(compass_db):
    for offset in range(2, 9):
        put_day(compass_db, RUN_DAY - timedelta(days=offset), spend=4_000)
    put_day(compass_db, YESTERDAY, spend=12_000)
    compass_db.commit()

    store.set_setting(compass_db, "signal.spend_anomaly_factor", "3,5")
    assert signals.evaluate(compass_db, RUN_DAY) == []  # 12000 ≤ 3,5 × 4000

    store.set_setting(compass_db, "signal.spend_anomaly_factor", "2,5")
    fired = signals.evaluate(compass_db, RUN_DAY)
    assert [s.type for s in fired] == ["spend_anomaly"]
    assert "2,5×" in fired[0].message


# ── threshold overrides change rule behaviour ────────────────────────


def test_mer_scale_factor_override_changes_scale_up_firing(compass_db):
    add_model(compass_db)
    seed_days(compass_db)
    set_stock(compass_db, 1000)

    # Demand 3,5× break-even instead of 1,2×: MER 6,00 < 7,00 → quiet.
    store.set_setting(compass_db, "signal.mer_scale_factor", "3,5")
    assert signals.evaluate(compass_db, RUN_DAY) == []

    store.set_setting(compass_db, "signal.mer_scale_factor", "2,9")
    fired = signals.evaluate(compass_db, RUN_DAY)  # 6,00 ≥ 5,80 → fires
    assert [s.type for s in fired] == ["scale_up"]
    assert "2,9×" in fired[0].explanation


# ── degraded modes ───────────────────────────────────────────────────


def test_without_cost_model_only_spend_anomaly_can_fire(compass_db):
    # Directly seeded metrics that would satisfy scale_up AND reorder…
    seed_days(compass_db)
    put_day(  # …plus a spend spike on yesterday (CAC still stable: 2625 ≤ 2625)
        compass_db, YESTERDAY,
        revenue_excl=30_000, margin=15_000, spend=12_000, units=10,
        new_customers=4, orders=10,
    )
    compass_db.commit()
    set_stock(compass_db, 100)

    fired = signals.evaluate(compass_db, RUN_DAY)

    assert [s.type for s in fired] == ["spend_anomaly"]
    assert signal_types(compass_db) == ["spend_anomaly"]


def test_empty_database_yields_no_signals_and_no_exception(compass_db):
    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []
    assert not compass_db.in_transaction

    # A cost model alone (no data yet) changes nothing.
    add_model(compass_db)
    assert signals.evaluate(compass_db, RUN_DAY) == []
    assert signal_types(compass_db) == []
