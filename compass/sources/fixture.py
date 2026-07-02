"""Fixture-backed sources: develop, test and demo without any credentials.

Items are raw API-shaped dicts (exactly what the live endpoints return)
and run through the SAME parse functions as the live adapters — fixture
mode and real mode can never drift apart. Used by tests, `compass verify`
(no-credentials fallback) and `compass demo` (via compass/demo.py).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from compass.models import AdSpendRecord, InventoryRecord, OrderRecord, VerifyReport
from compass.sources.base import AdSpendSource, InventorySource, OrdersSource
from compass.sources.bol import parse_order_detail
from compass.sources.meta_insights import parse_insight_row
from compass.sources.shopify import parse_order

DEFAULT_FIXTURE_DIR = Path("tests/fixtures/compass")

_FIXTURE_LINE = "Dit is demo-data (fixture) — geen echte cijfers."


def _load_items(path: Path, key: str) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get(key, [])


class FixtureShopifyOrders(OrdersSource):
    name = "fixture:shopify"

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items

    @classmethod
    def from_dir(cls, path: Path | str = DEFAULT_FIXTURE_DIR) -> "FixtureShopifyOrders":
        return cls(_load_items(Path(path) / "shopify_orders.json", "orders"))

    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        for item in self.items:
            record = parse_order(item)
            if record is None:  # Shopify test order
                continue
            if since <= record.order_day <= until:
                yield record

    def verify(self) -> VerifyReport:
        parsed = [r for r in map(parse_order, self.items) if r is not None]
        return VerifyReport(
            source=self.name,
            ok=True,
            mode="fixture",
            lines=[_FIXTURE_LINE, f"{len(parsed)} Shopify-voorbeeldorders geparsed."],
        )


class FixtureBolOrders(OrdersSource):
    name = "fixture:bol"

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items

    @classmethod
    def from_dir(cls, path: Path | str = DEFAULT_FIXTURE_DIR) -> "FixtureBolOrders":
        return cls(_load_items(Path(path) / "bol_orders.json", "orders"))

    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        for item in self.items:
            record = parse_order_detail(item)
            if since <= record.order_day <= until:
                yield record

    def verify(self) -> VerifyReport:
        return VerifyReport(
            source=self.name,
            ok=True,
            mode="fixture",
            lines=[_FIXTURE_LINE, f"{len(self.items)} bol-voorbeeldorders geparsed."],
        )


class FixtureMetaSpend(AdSpendSource):
    name = "fixture:meta"

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items

    @classmethod
    def from_dir(cls, path: Path | str = DEFAULT_FIXTURE_DIR) -> "FixtureMetaSpend":
        return cls(_load_items(Path(path) / "meta_insights.json", "data"))

    def fetch_daily_spend(self, since: date, until: date) -> Iterator[AdSpendRecord]:
        for row in self.items:
            record = parse_insight_row(row)
            if since <= record.day <= until:
                yield record

    def verify(self) -> VerifyReport:
        return VerifyReport(
            source=self.name,
            ok=True,
            mode="fixture",
            lines=[_FIXTURE_LINE, f"{len(self.items)} Meta campagne-dagen geparsed."],
        )


class FixtureInventory(InventorySource):
    name = "fixture:inventory"

    def __init__(self, records: list[InventoryRecord]):
        self.records = records

    def fetch_inventory(self) -> InventoryRecord | None:
        if not self.records:
            return None
        return max(self.records, key=lambda r: r.day)
