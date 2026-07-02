"""Source interfaces — every external data adapter implements one of these.

`since`/`until` are inclusive Europe/Amsterdam calendar dates (see
compass.models.ams_day): adapters translate them to whatever their API
expects, so callers never think in API timestamps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterator

from compass.models import AdSpendRecord, InventoryRecord, OrderRecord, VerifyReport


class SourceError(Exception):
    """A source-level failure (network, API error). Collector logs and continues."""


class CredentialsError(SourceError):
    """Missing or invalid credentials. Needs human action (see compass/README.md)."""


class OrdersSource(ABC):
    """A sales channel that can list its orders."""

    name: str  # 'shopify' | 'bol' (fixture twins use 'fixture:<name>')

    @abstractmethod
    def fetch_orders(self, since: date, until: date) -> Iterator[OrderRecord]:
        """Yield normalized orders for the inclusive Amsterdam-day window.

        Adapters may yield extra orders from *before* `since` (e.g. an old
        order re-fetched because a refund updated it) — the idempotent
        upsert makes those free. Orders created after `until` are never
        yielded.
        """

    @abstractmethod
    def verify(self) -> VerifyReport:
        """Health/coverage check in plain Dutch for `compass verify`.

        Reads only — a verify must never write to the database.
        """


class AdSpendSource(ABC):
    """An ad platform that can report our own spend per campaign per day."""

    name: str  # 'meta'

    @abstractmethod
    def fetch_daily_spend(self, since: date, until: date) -> Iterator[AdSpendRecord]:
        """Yield one record per campaign per day for the inclusive window.

        The platform's own purchase numbers ride along as meta_* fields:
        they are an attribution claim, never 'the revenue'.
        """

    @abstractmethod
    def verify(self) -> VerifyReport:
        """Health/coverage check in plain Dutch for `compass verify`.

        Reads only — a verify must never write to the database.
        """


class InventorySource(ABC):
    """A source that knows the current sellable stock of the one SKU."""

    name: str

    @abstractmethod
    def fetch_inventory(self) -> InventoryRecord | None:
        """Today's stock snapshot, or None when the source cannot tell
        (e.g. missing scope) — inventory is a nice-to-have that must
        never fail a collect run."""
