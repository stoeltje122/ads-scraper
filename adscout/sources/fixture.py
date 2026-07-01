"""Fixture-backed AdSource: develop and test without a Meta token.

Reads JSON files shaped exactly like /ads_archive responses
({"data": [...]}). Used by tests and by --source=fixture dry runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from adscout.models import AdRecord, PageCandidate
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
        wanted = {str(p) for p in page_ids}
        for item in self._load_all():
            if str(item.get("page_id")) in wanted:
                yield parse_ad(item)

    def search_pages(self, brand_name: str, country: str) -> list[PageCandidate]:
        needle = brand_name.lower()
        by_page: dict[str, PageCandidate] = {}
        for item in self._load_all():
            ad = parse_ad(item)
            name = (ad.page_name or "").lower()
            text = " ".join(t.body or "" for t in ad.texts).lower()
            if needle not in name and needle not in text:
                continue
            cand = by_page.setdefault(
                ad.page_id or "?",
                PageCandidate(page_id=ad.page_id or "?", page_name=ad.page_name or "?"),
            )
            cand.ads_seen += 1
            if not ad.ad_delivery_stop:
                cand.active_ads += 1
        return sorted(by_page.values(), key=lambda c: (-c.active_ads, -c.ads_seen))
