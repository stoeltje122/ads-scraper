"""The collect run: fetch orders/spend/inventory, rebuild the daily
rollup, evaluate signals, and always leave an audit-trail row.

Guarantees:
- Idempotent: every write is an ON CONFLICT upsert on a natural key, so
  re-running collect/backfill never duplicates data.
- Soft failure: the sources are independent APIs, so one failing source
  is recorded in the run result and never stops the others (unlike
  AdScout, where one broken token breaks every advertiser).
- The audit trail always gets a row — even when the run is refused
  because the cost model is missing — so `compass status` shows what
  happened, not only the log file.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from compass import queries, signals, store
from compass.config import Settings
from compass.models import Signal, ams_today
from compass.sources import (
    AdSpendSource,
    BolSource,
    CredentialsError,
    InventorySource,
    MetaInsightsSource,
    OrdersSource,
    ShopifySource,
    SourceError,
)

logger = logging.getLogger(__name__)

# The one refusal the collector issues itself: without a cost model the
# rebuild would fail halfway, so no API call is spent at all.
NO_COST_MODEL_ERROR = (
    "Geen kostenmodel — draai `compass init` of vul het kostenmodel in "
    "via het dashboard."
)

_SKIP_REASONS = {
    "shopify": (
        "geen credentials (zie compass/README.md, sectie "
        "'Shopify custom app aanmaken')"
    ),
    "meta": (
        "geen credentials (zie compass/README.md, sectie "
        "'Meta Marketing API koppelen')"
    ),
    "bol": (
        "geen credentials (zie compass/README.md, sectie "
        "'bol Retailer API koppelen')"
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SourceResult:
    """Outcome of one source in one run. `category` says which counter
    the upserts belong to (orders/spend/inventory) — names alone cannot
    tell, because tests and the demo inject arbitrarily named sources."""

    name: str
    ok: bool = True
    skipped: str | None = None       # Dutch reason; a skip is not a failure
    fetched: int = 0
    upserted: int = 0                # newly inserted rows (updates are free)
    error: str | None = None
    category: str = ""               # 'orders' | 'spend' | 'inventory'


@dataclass
class RunResult:
    kind: str = "collect"            # 'collect' | 'backfill' | 'import' | 'demo'
    started_at: str = ""
    finished_at: str = ""
    sources: list[SourceResult] = field(default_factory=list)
    signals_fired: list[Signal] = field(default_factory=list)
    run_id: int | None = None
    # Failures not tied to one source (missing cost model, a signals bug).
    run_errors: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        out = [f"{s.name}: {s.error}" for s in self.sources if s.error]
        out.extend(self.run_errors)
        return out

    @property
    def ok(self) -> bool:
        return not self.errors  # skips are fine; only real errors count

    @property
    def orders_upserted(self) -> int:
        return sum(s.upserted for s in self.sources if s.category == "orders")

    @property
    def spend_rows_upserted(self) -> int:
        return sum(s.upserted for s in self.sources if s.category == "spend")


def build_sources(
    settings: Settings | None,
) -> tuple[
    list[OrdersSource], list[AdSpendSource], list[InventorySource], list[SourceResult]
]:
    """Construct the live sources that have credentials.

    Unconfigured sources become a skipped SourceResult, so the run
    result (and `compass collect` output) always names all three — a
    silent hole in the data is worse than a visible skip. A
    CredentialsError raised at construction counts as a skip too: it
    needs a human, not an alarm.
    """
    orders: list[OrdersSource] = []
    spend: list[AdSpendSource] = []
    inventory: list[InventorySource] = []
    skipped: list[SourceResult] = []

    def skip(name: str, category: str, reason: str) -> None:
        skipped.append(SourceResult(name=name, skipped=reason, category=category))
        logger.info("Bron %s overgeslagen: %s", name, reason)

    if settings is not None and settings.has_shopify():
        try:
            shopify = ShopifySource(settings)
        except CredentialsError as exc:
            skip("shopify", "orders", str(exc))
        else:
            orders.append(shopify)
            inventory.append(shopify)  # one instance, one client, two roles
    else:
        skip("shopify", "orders", _SKIP_REASONS["shopify"])

    if settings is not None and settings.has_meta():
        try:
            spend.append(MetaInsightsSource(settings))
        except CredentialsError as exc:
            skip("meta", "spend", str(exc))
    else:
        skip("meta", "spend", _SKIP_REASONS["meta"])

    if settings is not None and settings.has_bol():
        try:
            orders.append(BolSource(settings))
        except CredentialsError as exc:
            skip("bol", "orders", str(exc))
    else:
        skip("bol", "orders", _SKIP_REASONS["bol"])

    return orders, spend, inventory, skipped


def _fetch_guarded(source_result: SourceResult, fetch: Callable[[], None]) -> None:
    """Run one source's fetch under the three-tier soft-failure policy.

    Records already stored before the exception are kept: partial data
    plus a visible error beats losing the morning's numbers.
    """
    try:
        fetch()
    except CredentialsError as exc:
        source_result.ok = False
        source_result.error = f"credentials-probleem: {exc}"
        logger.error(
            "Bron %s: credentials-probleem — run gaat door: %s",
            source_result.name, exc,
        )
    except SourceError as exc:
        source_result.ok = False
        source_result.error = str(exc)
        logger.error("Bron %s faalde: %s — run gaat door", source_result.name, exc)
    except Exception as exc:  # defensive: a parse bug in one source
        source_result.ok = False
        source_result.error = f"onverwachte fout: {exc}"
        logger.exception(
            "Onverwachte fout bij bron %s — run gaat door", source_result.name
        )


def collect(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    since: date | None = None,
    until: date | None = None,
    today: date | None = None,
    kind: str = "collect",
    orders_sources: list[OrdersSource] | None = None,
    spend_sources: list[AdSpendSource] | None = None,
    inventory_sources: list[InventorySource] | None = None,
) -> RunResult:
    """Run one collect/backfill pass and record it in the audit trail.

    The default window is the last three days: recent orders may still
    gain refunds, and the idempotent upserts make the overlap free.
    Source overrides (tests, demo) replace build_sources entirely, so
    those callers never touch the network.
    """
    today = today or ams_today()
    until = until or today
    since = since or today - timedelta(days=3)

    result = RunResult(kind=kind, started_at=_now_iso())

    # Guard before any fetching: without a cost model the daily rebuild
    # cannot run, so the whole run would be wasted API calls.
    if queries.cost_model_for(conn, today) is None:
        result.run_errors.append(NO_COST_MODEL_ERROR)
        logger.error("Run geweigerd: %s", NO_COST_MODEL_ERROR)
        result.finished_at = _now_iso()
        result.run_id = store.record_run(conn, result)
        return result

    overridden = (
        orders_sources is not None
        or spend_sources is not None
        or inventory_sources is not None
    )
    if overridden:
        orders_sources = orders_sources or []
        spend_sources = spend_sources or []
        inventory_sources = inventory_sources or []
    else:
        orders_sources, spend_sources, inventory_sources, skipped = build_sources(
            settings
        )
        result.sources.extend(skipped)

    # The rebuild must cover every day an upsert touched: a re-fetched
    # refund can update an order far older than `since`.
    earliest_day: date | None = None

    def touch(day: date) -> None:
        nonlocal earliest_day
        if earliest_day is None or day < earliest_day:
            earliest_day = day

    for source in orders_sources:
        source_result = SourceResult(name=source.name, category="orders")
        result.sources.append(source_result)

        def fetch_orders(source=source, source_result=source_result) -> None:
            for record in source.fetch_orders(since, until):
                source_result.fetched += 1
                if store.upsert_order(conn, record):
                    source_result.upserted += 1
                touch(record.order_day)

        _fetch_guarded(source_result, fetch_orders)
        # Commit per source (AdScout discipline): a killed process during a
        # long backfill keeps every source that already finished.
        conn.commit()

    for source in spend_sources:
        source_result = SourceResult(name=source.name, category="spend")
        result.sources.append(source_result)

        def fetch_spend(source=source, source_result=source_result) -> None:
            for record in source.fetch_daily_spend(since, until):
                source_result.fetched += 1
                if store.upsert_ad_spend(conn, record):
                    source_result.upserted += 1
                touch(record.day)

        _fetch_guarded(source_result, fetch_spend)
        conn.commit()

    for source in inventory_sources:
        source_result = SourceResult(name=source.name, category="inventory")
        result.sources.append(source_result)

        def fetch_inventory(source=source, source_result=source_result) -> None:
            record = source.fetch_inventory()
            if record is not None:  # None = source cannot tell; not an error
                source_result.fetched = 1
                store.upsert_inventory(conn, record)
                source_result.upserted = 1

        _fetch_guarded(source_result, fetch_inventory)
        conn.commit()

    rebuild_start = min(since, earliest_day) if earliest_day else since
    try:
        # Atomic inside (rolls itself back on failure); the per-source
        # commits above mean a rollback can never take fetched data along.
        store.rebuild_daily_metrics(conn, rebuild_start, today)
    except Exception as exc:  # data is stored; the rollup can be rebuilt later
        result.run_errors.append(f"dagcijfers herbouwen mislukt: {exc}")
        logger.exception("Dagcijfers herbouwen mislukt — data is wel opgeslagen")
    else:
        # Signals read the rollup just rebuilt. A bug in a rule must
        # never take the day's data down with it.
        try:
            result.signals_fired = signals.evaluate(conn, today)
        except Exception as exc:
            result.run_errors.append(f"signalen bepalen mislukt: {exc}")
            logger.exception("Signalen bepalen mislukt — data is wel opgeslagen")

    conn.commit()
    result.finished_at = _now_iso()
    result.run_id = store.record_run(conn, result)
    return result
