"""collector: source construction from Settings, the cost-model guard,
the three-tier soft failure, the rebuild window, signal evaluation and
the runs audit trail. All offline: fixture sources + local stubs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator

import pytest

from conftest import RUN_DAY, TESTS_DIR

from compass import collector, queries, store
from compass.config import Settings
from compass.models import (
    AMSTERDAM,
    CostModel,
    InventoryRecord,
    OrderRecord,
    PaymentFee,
    Signal,
    VerifyReport,
)
from compass.sources.base import CredentialsError, OrdersSource, SourceError
from compass.sources.fixture import (
    FixtureBolOrders,
    FixtureInventory,
    FixtureMetaSpend,
    FixtureShopifyOrders,
)

FIXTURE_DIR = TESTS_DIR / "fixtures" / "compass"
SINCE = date(2026, 6, 1)

# The committed fixtures: 5 usable Shopify orders (one test order is
# skipped), 4 bol orders, 6 Meta campaign-days — all June 2026.
FIXTURE_SHOPIFY_ORDERS = 5
FIXTURE_BOL_ORDERS = 4
FIXTURE_META_ROWS = 6
FIXTURE_ORDERS = FIXTURE_SHOPIFY_ORDERS + FIXTURE_BOL_ORDERS


def seed_cost_model(conn, valid_from: date = date(2026, 1, 1)) -> None:
    store.add_cost_model(
        conn,
        CostModel(
            valid_from=valid_from,
            cogs_per_unit_cents=650,
            shipping_per_order_cents=440,
            fee_pct=0.015,
            fee_fixed_cents=25,
            payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
            bol_commission_pct=0.124,
            vat_rate=0.09,
            fixed_month_cents=40_000,
        ),
    )


def fixture_kwargs() -> dict:
    """Source overrides pointing at the committed fixture files."""
    return dict(
        orders_sources=[
            FixtureShopifyOrders.from_dir(FIXTURE_DIR),
            FixtureBolOrders.from_dir(FIXTURE_DIR),
        ],
        spend_sources=[FixtureMetaSpend.from_dir(FIXTURE_DIR)],
        inventory_sources=[
            FixtureInventory(
                [InventoryRecord(day=RUN_DAY - timedelta(days=1), units=500,
                                 source="fixture")]
            )
        ],
    )


def make_order(extern_id: str, day: date, gross: int = 2995) -> OrderRecord:
    return OrderRecord(
        extern_id=extern_id,
        channel="shopify",
        ordered_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=AMSTERDAM),
        gross_cents=gross,
        payment_method="ideal",
    )


class StubOrders(OrdersSource):
    """Orders source that yields canned records, then optionally fails."""

    name = "stub:orders"

    def __init__(self, records=(), exc: Exception | None = None, name: str | None = None):
        self.records = list(records)
        self.exc = exc
        self.calls = 0
        self.windows: list[tuple[date, date]] = []
        if name:
            self.name = name

    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        self.calls += 1
        self.windows.append((since, until))
        yield from self.records
        if self.exc is not None:
            raise self.exc

    def verify(self) -> VerifyReport:
        return VerifyReport(source=self.name, ok=True, mode="fixture")


@pytest.fixture
def collected(compass_db):
    """A database after one fixture collect on the fixed run day."""
    seed_cost_model(compass_db)
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY, **fixture_kwargs()
    )
    return compass_db, result


# ── build_sources ────────────────────────────────────────────────────


def test_build_sources_skips_all_three_without_credentials_in_dutch():
    orders, spend, inventory, skipped = collector.build_sources(Settings())
    assert orders == [] and spend == [] and inventory == []
    assert [s.name for s in skipped] == ["shopify", "meta", "bol"]
    for source_result in skipped:
        assert source_result.skipped is not None
        assert "geen credentials" in source_result.skipped
        assert "README" in source_result.skipped
        assert source_result.ok and source_result.error is None


def test_build_sources_constructs_every_configured_source():
    settings = Settings(
        shopify_shop="cloudplunge.myshopify.com",
        shopify_access_token="shpat_test",
        meta_access_token="token",
        meta_ad_account_id="act_123",
        bol_client_id="client",
        bol_client_secret="geheim",
    )
    orders, spend, inventory, skipped = collector.build_sources(settings)
    assert [s.name for s in orders] == ["shopify", "bol"]
    assert [s.name for s in spend] == ["meta"]
    assert [s.name for s in inventory] == ["shopify"]
    # One Shopify instance plays both roles: one client, one rate budget.
    assert orders[0] is inventory[0]
    assert skipped == []


def test_build_sources_with_partial_configuration_mixes_live_and_skipped():
    settings = Settings(
        shopify_shop="cloudplunge.myshopify.com", shopify_access_token="shpat_test"
    )
    orders, spend, inventory, skipped = collector.build_sources(settings)
    assert [s.name for s in orders] == ["shopify"]
    assert spend == []
    assert {s.name for s in skipped} == {"meta", "bol"}


# ── the cost-model guard ─────────────────────────────────────────────


def test_collect_without_cost_model_refuses_fetches_nothing_but_records_the_run(
    compass_db,
):
    stub = StubOrders([make_order("s-1", date(2026, 6, 20))])
    result = collector.collect(
        compass_db, today=RUN_DAY,
        orders_sources=[stub], spend_sources=[], inventory_sources=[],
    )
    assert not result.ok
    assert result.errors == [collector.NO_COST_MODEL_ERROR]
    assert "Geen kostenmodel" in result.errors[0]
    assert stub.calls == 0, "guard must refuse before any fetching"
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    runs = queries.last_runs(compass_db)
    assert len(runs) == 1
    assert runs[0]["ok"] == 0
    assert result.run_id == runs[0]["id"]


# ── the happy path on fixture data ───────────────────────────────────


def test_collect_stores_fixture_orders_spend_and_inventory(collected):
    conn, result = collected
    assert result.ok
    assert result.kind == "collect"
    assert result.orders_upserted == FIXTURE_ORDERS
    assert result.spend_rows_upserted == FIXTURE_META_ROWS
    assert [s.name for s in result.sources] == [
        "fixture:shopify", "fixture:bol", "fixture:meta", "fixture:inventory",
    ]
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == FIXTURE_ORDERS
    assert (
        conn.execute("SELECT COUNT(*) FROM ad_spend_daily").fetchone()[0]
        == FIXTURE_META_ROWS
    )
    inventory = queries.latest_inventory(conn)
    assert inventory is not None and inventory.units == 500


def test_collect_records_a_successful_run_in_the_audit_trail(collected):
    conn, result = collected
    runs = queries.last_runs(conn)
    assert len(runs) == 1
    run = runs[0]
    assert result.run_id == run["id"]
    assert run["ok"] == 1
    assert run["kind"] == "collect"
    assert run["orders_upserted"] == FIXTURE_ORDERS
    assert run["spend_rows_upserted"] == FIXTURE_META_ROWS
    assert run["started_at"] and run["finished_at"]


def test_collect_rebuilds_daily_metrics_for_every_day_in_the_window(collected):
    conn, _ = collected
    rows = queries.day_rows(conn, SINCE, RUN_DAY)
    assert len(rows) == 31  # June 1 .. July 1, one row per day, gaps included
    assert rows[0]["day"] == "2026-06-01"
    assert rows[-1]["day"] == "2026-07-01"
    june_5 = next(row for row in rows if row["day"] == "2026-06-05")
    assert june_5["orders_count"] >= 1
    assert june_5["revenue_shopify_cents"] > 0


def test_collect_defaults_to_a_three_day_overlap_window(compass_db):
    seed_cost_model(compass_db)
    spy = StubOrders()
    result = collector.collect(
        compass_db, today=RUN_DAY,
        orders_sources=[spy], spend_sources=[], inventory_sources=[],
    )
    assert result.ok
    assert spy.windows == [(RUN_DAY - timedelta(days=3), RUN_DAY)]


def test_collect_with_settings_but_no_credentials_skips_everything_yet_stays_ok(
    compass_db,
):
    seed_cost_model(compass_db)
    result = collector.collect(compass_db, Settings(), today=RUN_DAY)
    assert result.ok, "skips are not errors"
    assert [s.name for s in result.sources] == ["shopify", "meta", "bol"]
    assert all(s.skipped for s in result.sources)
    assert result.orders_upserted == 0
    assert queries.last_runs(compass_db)[0]["ok"] == 1


# ── idempotency ──────────────────────────────────────────────────────


def test_collect_twice_is_idempotent_and_counts_nothing_as_new(collected):
    conn, _ = collected
    second = collector.collect(
        conn, today=RUN_DAY, since=SINCE, until=RUN_DAY, **fixture_kwargs()
    )
    assert second.ok
    assert second.orders_upserted == 0
    assert second.spend_rows_upserted == 0
    shopify = next(s for s in second.sources if s.name == "fixture:shopify")
    assert shopify.fetched == FIXTURE_SHOPIFY_ORDERS  # re-fetched, none new
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == FIXTURE_ORDERS
    assert (
        conn.execute("SELECT COUNT(*) FROM ad_spend_daily").fetchone()[0]
        == FIXTURE_META_ROWS
    )
    assert len(queries.last_runs(conn)) == 2


# ── soft failure tiers ───────────────────────────────────────────────


def test_one_failing_source_never_stops_the_others(compass_db):
    seed_cost_model(compass_db)
    failing = StubOrders(
        records=[make_order("s-1", date(2026, 6, 20))],
        exc=SourceError("API kapot (status 500)"),
    )
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY,
        orders_sources=[failing, FixtureBolOrders.from_dir(FIXTURE_DIR)],
        spend_sources=[FixtureMetaSpend.from_dir(FIXTURE_DIR)],
        inventory_sources=[],
    )
    assert not result.ok
    assert result.errors == ["stub:orders: API kapot (status 500)"]
    # Records yielded before the crash are kept: partial data plus a
    # visible error beats losing the morning's numbers.
    assert (
        compass_db.execute(
            "SELECT COUNT(*) FROM orders WHERE channel = 'shopify'"
        ).fetchone()[0]
        == 1
    )
    assert (
        compass_db.execute(
            "SELECT COUNT(*) FROM orders WHERE channel = 'bol'"
        ).fetchone()[0]
        == FIXTURE_BOL_ORDERS
    )
    assert result.spend_rows_upserted == FIXTURE_META_ROWS
    assert queries.last_runs(compass_db)[0]["ok"] == 0


def test_credentials_error_during_fetch_is_labelled_and_run_continues(compass_db):
    seed_cost_model(compass_db)
    broken = StubOrders(exc=CredentialsError("token verlopen"), name="stub:broken")
    healthy = StubOrders(
        records=[make_order("s-2", date(2026, 6, 21))], name="stub:healthy"
    )
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY,
        orders_sources=[broken, healthy], spend_sources=[], inventory_sources=[],
    )
    assert result.errors == ["stub:broken: credentials-probleem: token verlopen"]
    assert healthy.calls == 1, "sources are independent APIs; the run continues"
    assert result.orders_upserted == 1


def test_unexpected_exception_is_recorded_as_onverwachte_fout(compass_db):
    seed_cost_model(compass_db)
    buggy = StubOrders(exc=RuntimeError("kaboom"))
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY,
        orders_sources=[buggy], spend_sources=[], inventory_sources=[],
    )
    assert result.errors == ["stub:orders: onverwachte fout: kaboom"]
    assert queries.last_runs(compass_db)[0]["ok"] == 0


# ── rebuild window ───────────────────────────────────────────────────


def test_rebuild_extends_back_to_the_earliest_touched_day(compass_db):
    """A re-fetched refund can touch an order far older than `since`;
    the rollup must be rebuilt from that day, not from `since`."""
    seed_cost_model(compass_db)
    old_day = date(2026, 5, 20)
    stub = StubOrders([make_order("oud-1", old_day)])
    result = collector.collect(
        compass_db, today=RUN_DAY, since=date(2026, 6, 20),
        orders_sources=[stub], spend_sources=[], inventory_sources=[],
    )
    assert result.ok
    row = compass_db.execute(
        "SELECT * FROM daily_metrics WHERE day = ?", (old_day.isoformat(),)
    ).fetchone()
    assert row is not None and row["orders_count"] == 1


# ── signals integration ──────────────────────────────────────────────


def test_signals_evaluate_after_the_rebuild_and_land_in_the_result(
    compass_db, monkeypatch
):
    seed_cost_model(compass_db)
    seen: dict = {}

    def spy(conn, today):
        seen["metrics_rows"] = conn.execute(
            "SELECT COUNT(*) FROM daily_metrics"
        ).fetchone()[0]
        seen["today"] = today
        return [Signal(type="reorder", day=today, message="stub")]

    monkeypatch.setattr(collector.signals, "evaluate", spy)
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY, **fixture_kwargs()
    )
    assert seen["today"] == RUN_DAY
    assert seen["metrics_rows"] == 31, "the rollup must exist before signals run"
    assert [s.type for s in result.signals_fired] == ["reorder"]


def test_a_signals_crash_records_an_error_but_keeps_the_data(compass_db, monkeypatch):
    seed_cost_model(compass_db)

    def explode(conn, today):
        raise RuntimeError("signaalbug")

    monkeypatch.setattr(collector.signals, "evaluate", explode)
    result = collector.collect(
        compass_db, today=RUN_DAY, since=SINCE, until=RUN_DAY, **fixture_kwargs()
    )
    assert not result.ok
    assert any("signalen" in error and "signaalbug" in error for error in result.errors)
    assert result.signals_fired == []
    # The data and the rollup survived the signals bug.
    assert (
        compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        == FIXTURE_ORDERS
    )
    assert len(queries.day_rows(compass_db, SINCE, RUN_DAY)) == 31
    assert queries.last_runs(compass_db)[0]["ok"] == 0
