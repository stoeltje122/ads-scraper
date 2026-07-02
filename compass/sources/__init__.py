"""Compass data sources (adapter pattern).

The interfaces live in base.py; every external system has one read-only
live adapter plus a fixture-backed twin, so the whole pipeline runs and
demos without any credentials.
"""

from compass.sources.base import (
    AdSpendSource,
    CredentialsError,
    InventorySource,
    OrdersSource,
    SourceError,
)
from compass.sources.shopify import ShopifySource
from compass.sources.meta_insights import MetaInsightsSource
from compass.sources.bol import BolSource
from compass.sources.fixture import (
    FixtureBolOrders,
    FixtureInventory,
    FixtureMetaSpend,
    FixtureShopifyOrders,
)

__all__ = [
    "SourceError",
    "CredentialsError",
    "OrdersSource",
    "AdSpendSource",
    "InventorySource",
    "ShopifySource",
    "MetaInsightsSource",
    "BolSource",
    "FixtureShopifyOrders",
    "FixtureMetaSpend",
    "FixtureBolOrders",
    "FixtureInventory",
]
