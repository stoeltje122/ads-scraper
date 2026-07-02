"""Fixture-backed AdSource: develop and test without a Meta token.

Reads JSON files shaped exactly like /ads_archive responses
({"data": [...]}). Used by tests and by --source=fixture dry runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from adscout.models import AdRecord, PageCandidate, aggregate_candidates
from adscout.sources.base import AdSource
from adscout.sources.meta import parse_ad


class FixtureAdSource(AdSource):
    def __init__(self, fixture_dir: Path | str):
        self.fixture_dir = Path(fixture_dir)

    def _load_all(self) -> list[dict]:
        items: list[dict] = []
        for path in sorted(self.fixture_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.extend(payload.get("data", []))
        return items

    def fetch_ads(
        self,
        page_ids: list[str],
        country: str,
        active_status: str = "ALL",
    ) -> Iterator[AdRecord]:
        from adscout.models import utc_today

        wanted = {str(p) for p in page_ids}
        today = utc_today()
        for item in self._load_all():
            if str(item.get("page_id")) not in wanted:
                continue
            record = parse_ad(item)
            if active_status == "ACTIVE" and not record.is_active(today):
                continue
            if active_status == "INACTIVE" and record.is_active(today):
                continue
            yield record

    def search_pages(self, brand_name: str, country: str) -> list[PageCandidate]:
        needle = brand_name.lower()

        def matches(ad: AdRecord) -> bool:
            name = (ad.page_name or "").lower()
            text = " ".join(t.body or "" for t in ad.texts).lower()
            return needle in name or needle in text

        records = [ad for ad in map(parse_ad, self._load_all()) if matches(ad)]
        return aggregate_candidates(records)
