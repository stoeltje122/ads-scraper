"""Normalized data records passed between sources, collector and storage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class AdTextVariant:
    body: str | None = None
    title: str | None = None
    caption: str | None = None
    description: str | None = None


@dataclass
class AdRecord:
    """One ad from a source, normalized. `raw` keeps the untouched payload."""

    ad_archive_id: str
    page_id: str | None
    page_name: str | None
    ad_creation_time: str | None
    ad_delivery_start: str | None
    ad_delivery_stop: str | None
    snapshot_url: str | None
    platforms: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    texts: list[AdTextVariant] = field(default_factory=list)
    eu_reach: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def is_active(self, on_day: date) -> bool:
        """Active = no delivery stop, or stop on/after `on_day`.

        Commercial ads expose no explicit status field; this is the standard
        inference from ad_delivery_stop_time.
        """
        if not self.ad_delivery_stop:
            return True
        return _parse_day(self.ad_delivery_stop) >= on_day

    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)


@dataclass
class PageCandidate:
    """A candidate Facebook page for a brand name, found by the resolver."""

    page_id: str
    page_name: str
    ads_seen: int = 0
    active_ads: int = 0
    example_text: str | None = None


def _parse_day(value: str) -> date:
    """Parse the date part of a Meta timestamp ('2024-05-01' or ISO datetime)."""
    return date.fromisoformat(value[:10])


def runtime_days(
    delivery_start: str | None,
    delivery_stop: str | None,
    last_seen: str | None,
) -> int | None:
    """Runtime in days: (delivery_stop or last_seen) − delivery_start.

    Our winner metric: commercial ads expose no spend/impressions, so a long
    runtime is the best available proxy for "this ad works".
    """
    if not delivery_start:
        return None
    end = delivery_stop or last_seen
    if not end:
        return None
    days = (_parse_day(end) - _parse_day(delivery_start)).days
    return max(days, 0)
