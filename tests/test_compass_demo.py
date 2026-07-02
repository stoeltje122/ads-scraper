"""demo: deterministic generation, API-shaped payloads, and the tuned
outcomes after a full load — reorder fires while scale-up is suppressed
by the inventory guard, the attribution gap is 15–30%, margins positive."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import RUN_DAY

from compass import demo, metrics, queries
from compass.sources.shopify import parse_order

DAYS = 90
SINCE = RUN_DAY - timedelta(days=DAYS)
PROSPECTING_ID = demo.CAMPAIGNS[0][0]


@pytest.fixture
def loaded(compass_db):
    """A compass database after one demo load on the fixed run day."""
    result = demo.load_demo(compass_db, today=RUN_DAY)
    return compass_db, result


# ── generate(): determinism & shapes ─────────────────────────────────


def test_generate_is_deterministic_for_equal_inputs():
    assert demo.generate(RUN_DAY) == demo.generate(RUN_DAY)


def test_generate_differs_for_another_seed_or_day():
    base = demo.generate(RUN_DAY)
    assert base != demo.generate(RUN_DAY, seed=7)
    assert base != demo.generate(RUN_DAY - timedelta(days=1))


def test_shopify_items_are_api_shaped_and_parse_within_the_window():
    data = demo.generate(RUN_DAY)
    assert len(data.shopify_items) > 300
    for item in data.shopify_items:
        # The exact keys parse_order and the fixture files rely on.
        for key in (
            "financial_status",
            "refunds",
            "customer",
            "payment_gateway_names",
            "taxes_included",
            "total_price",
            "total_tax",
        ):
            assert key in item, f"missing {key}"
        assert item["created_at"].endswith(("+02:00", "+01:00"))
        record = parse_order(item)
        assert record is not None
        assert SINCE <= record.order_day <= RUN_DAY - timedelta(days=1)
    statuses = {item["financial_status"] for item in data.shopify_items}
    assert {"paid", "refunded", "partially_refunded"} <= statuses
    gateways = {item["payment_gateway_names"][0] for item in data.shopify_items}
    assert {"iDEAL", "shopify_payments", "klarna"} <= gateways


def test_bol_items_are_order_detail_shaped_with_one_cancellation():
    data = demo.generate(RUN_DAY)
    assert len(data.bol_items) > 200
    cancelled = 0
    for detail in data.bol_items:
        assert detail["orderId"]
        assert detail["orderPlacedDateTime"].endswith(("+02:00", "+01:00"))
        item = detail["orderItems"][0]
        assert item["unitPrice"] == 29.95
        if item["quantityCancelled"] == item["quantity"]:
            cancelled += 1
    assert cancelled == 1


def test_meta_rows_mirror_the_insights_response_shape():
    data = demo.generate(RUN_DAY)
    assert len(data.meta_rows) == DAYS * len(demo.CAMPAIGNS)
    for row in data.meta_rows:
        assert row["date_start"] == row["date_stop"]
        assert isinstance(row["actions"], list)
        assert isinstance(row["action_values"], list)
        assert "." in row["spend"]  # API decimal string, not cents
    # Realism: some rows claim purchases (via omni_purchase like the real
    # API) and some claim none at all.
    def claims(row):
        return any(a["action_type"] == "omni_purchase" for a in row["actions"])

    assert any(claims(row) for row in data.meta_rows)
    assert any(not claims(row) for row in data.meta_rows)


def test_cost_models_are_two_demo_versions():
    data = demo.generate(RUN_DAY)
    assert [cm.note for cm in data.cost_models] == ["DEMO", "DEMO"]
    assert data.cost_models[0].valid_from == SINCE
    assert data.cost_models[0].cogs_per_unit_cents == 700
    assert data.cost_models[1].valid_from == RUN_DAY - timedelta(days=45)
    assert data.cost_models[1].cogs_per_unit_cents == 650


def test_inventory_history_stays_positive_and_shows_the_restock():
    data = demo.generate(RUN_DAY)
    assert len(data.inventory) == DAYS
    assert all(rec.source == "fixture" for rec in data.inventory)
    assert all(rec.units > 0 for rec in data.inventory)
    by_day = {rec.day: rec.units for rec in data.inventory}
    restock_day = RUN_DAY - timedelta(days=demo.RESTOCK_DAYS_AGO)
    jump = by_day[restock_day] - by_day[restock_day - timedelta(days=1)]
    assert jump > 1000  # +1500 minus that day's sales


# ── load_demo(): the tuned outcomes on a real database ───────────────


def test_load_demo_runs_the_full_collect_path(loaded):
    conn, result = loaded
    assert result.kind == "demo"
    assert result.ok
    assert queries.last_runs(conn), "run must land in the audit trail"
    counts = {
        row["channel"]: row["n"]
        for row in conn.execute("SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel")
    }
    assert counts["shopify"] > 300
    assert counts["bol"] > 200


def test_load_demo_fills_at_least_88_days_of_daily_metrics(loaded):
    conn, _ = loaded
    filled = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE orders_count > 0"
    ).fetchone()[0]
    assert filled >= 88


def test_demo_business_makes_a_positive_margin(loaded):
    conn, _ = loaded
    totals = queries.window_totals(conn, SINCE, RUN_DAY)
    assert totals.margin_cents > 0
    assert totals.revenue_shopify_cents > 0
    assert totals.revenue_bol_cents > 0


def test_meta_claim_stays_below_reality_with_a_15_to_30_percent_gap(loaded):
    conn, _ = loaded
    totals = queries.window_totals(conn, SINCE, RUN_DAY)
    assert totals.meta_purchase_value_cents < totals.revenue_incl_cents
    # Meta's claim is measured against the Shopify shop it advertises for.
    gap = 1 - totals.meta_purchase_value_cents / totals.revenue_shopify_cents
    assert 0.15 <= gap <= 0.30


def test_reorder_fires_and_scale_up_is_suppressed(loaded):
    conn, result = loaded
    fired = {sig.type for sig in result.signals_fired}
    assert "reorder" in fired
    active = {sig.type for sig in queries.active_signals(conn)}
    assert "reorder" in active
    all_types = {sig.type for sig in queries.all_signals(conn)}
    assert "scale_up" not in all_types
    assert "scale_down" not in all_types
    # The engine itself recorded that the guard (not weak ad numbers)
    # blocked the scale-up — the cross-rule the demo exists to show.
    reorder = next(sig for sig in queries.active_signals(conn) if sig.type == "reorder")
    assert reorder.details.get("scale_up_suppressed") is True


def test_scale_up_conditions_genuinely_hold_so_absence_proves_the_guard(loaded):
    """The demo's point: the ad numbers justify scaling up, and ONLY the
    inventory guard holds it back. Verified numerically, not by wording."""
    conn, _ = loaded
    for offset in range(1, 8):  # every evaluation day of the streak
        d = RUN_DAY - timedelta(days=offset)
        window = queries.window_totals(conn, d - timedelta(days=6), d)
        assert window.spend_cents >= 7 * 2500
        assert window.mer is not None and window.break_even_roas is not None
        assert window.mer >= 1.2 * window.break_even_roas
    c_now = queries.window_totals(
        conn, RUN_DAY - timedelta(days=7), RUN_DAY - timedelta(days=1)
    ).cac_cents
    c_prev = queries.window_totals(
        conn, RUN_DAY - timedelta(days=14), RUN_DAY - timedelta(days=8)
    ).cac_cents
    assert c_now is not None and c_prev is not None
    assert c_now <= 1.05 * c_prev
    # ... and the guard condition: stock below the reorder threshold.
    inventory = queries.latest_inventory(conn)
    rate = metrics.weighted_daily_sales(
        queries.units_sold_by_day(
            conn, RUN_DAY - timedelta(days=30), RUN_DAY - timedelta(days=1)
        )
    )
    days_left = metrics.days_of_stock(inventory.units, rate)
    assert days_left is not None
    assert 20 < days_left < 30 * 1.3  # low enough to fire, not absurd


def test_spend_anomaly_is_visible_in_the_ad_spend_history(loaded):
    conn, _ = loaded

    def prospecting_spend(day):
        row = conn.execute(
            "SELECT spend_cents FROM ad_spend_daily WHERE day = ? AND campaign_id = ?",
            (day.isoformat(), PROSPECTING_ID),
        ).fetchone()
        return row["spend_cents"]

    anomaly = prospecting_spend(RUN_DAY - timedelta(days=demo.ANOMALY_DAYS_AGO))
    day_before = prospecting_spend(RUN_DAY - timedelta(days=demo.ANOMALY_DAYS_AGO + 1))
    day_after = prospecting_spend(RUN_DAY - timedelta(days=demo.ANOMALY_DAYS_AGO - 1))
    assert anomaly > 2 * day_before
    assert anomaly > 2 * day_after


def test_load_demo_twice_is_idempotent(loaded):
    conn, _ = loaded
    orders_before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    demo.load_demo(conn, today=RUN_DAY)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == orders_before
    assert len(queries.cost_model_history(conn)) == 2
    assert {sig.type for sig in queries.all_signals(conn)} >= {"reorder"}
    assert "scale_up" not in {sig.type for sig in queries.all_signals(conn)}
