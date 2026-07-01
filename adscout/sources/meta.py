"""Meta Ad Library API client (Graph API /ads_archive).

Conservative by design: this feeds a daily batch job, not a realtime system.
Rate limiting: exponential backoff on errors + slowing down when the
X-App-Usage header reports high usage.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from adscout.config import ADS_ARCHIVE_FIELDS, Settings
from adscout.models import AdRecord, AdTextVariant, PageCandidate, aggregate_candidates
from adscout.sources.base import AdSource, AdSourceError, TokenError

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGE_IDS_PER_CALL = 10  # documented API maximum for search_page_ids
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5
USAGE_SOFT_LIMIT = 80  # % of app quota; above this we pause between calls

# Meta Graph API error codes
ERROR_TOKEN = 190
RATE_LIMIT_CODES = {4, 17, 32, 613}

TOKEN_HELP = (
    "Je META_ACCESS_TOKEN is verlopen of ongeldig (Meta error 190). "
    "Long-lived tokens verlopen na ±60 dagen. Vernieuw het token en zet het "
    "in .env — zie README, sectie 'Token vernieuwen'."
)


class MetaAdLibraryAPI(AdSource):
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.meta_access_token:
            raise TokenError(
                "Geen META_ACCESS_TOKEN gevonden in .env. "
                "Zie README, sectie 'Meta developer-app en token'."
            )
        self.settings = settings
        self.client = client or httpx.Client(timeout=30.0)

    # ── AdSource interface ────────────────────────────────────────────

    def fetch_ads(
        self,
        page_ids: list[str],
        country: str,
        active_status: str = "ALL",
    ) -> Iterator[AdRecord]:
        # search_page_ids accepts up to 10 IDs per call → chunk.
        for i in range(0, len(page_ids), MAX_PAGE_IDS_PER_CALL):
            chunk = page_ids[i : i + MAX_PAGE_IDS_PER_CALL]
            params = self._base_params(country, active_status)
            params["search_page_ids"] = json.dumps(chunk)
            yield from (parse_ad(item) for item in self._paginate(params))

    def search_pages(self, brand_name: str, country: str) -> list[PageCandidate]:
        """Aggregate distinct advertising pages from a search_terms query.

        This only surfaces pages that actually run (or recently ran) ads —
        exactly what we want for a watchlist. The Ad Library web UI is the
        documented fallback for brands that show nothing here.
        """
        params = self._base_params(country, "ALL")
        params["search_terms"] = brand_name
        records = [parse_ad(item) for item in self._paginate(params, max_items=800)]
        candidates = aggregate_candidates(records)
        logger.info(
            "search_pages(%r, %s): %d ads, %d pages",
            brand_name, country, len(records), len(candidates),
        )
        return candidates

    # ── HTTP plumbing ─────────────────────────────────────────────────

    def _base_params(self, country: str, active_status: str) -> dict[str, Any]:
        return {
            "access_token": self.settings.meta_access_token,
            "ad_reached_countries": json.dumps([country]),  # required per call
            "ad_active_status": active_status,
            "ad_type": "ALL",
            "fields": ",".join(ADS_ARCHIVE_FIELDS),
            "limit": PAGE_SIZE,
        }

    def _paginate(
        self, params: dict[str, Any], max_items: int | None = None
    ) -> Iterator[dict[str, Any]]:
        url: str | None = f"{self.settings.graph_base_url}/ads_archive"
        request_params: dict[str, Any] | None = params
        yielded = 0
        while url:
            data = self._get(url, request_params)
            for item in data.get("data", []):
                yield item
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            # paging.next is a complete URL including our params & cursor
            url = data.get("paging", {}).get("next")
            request_params = None

    def _get(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning("Retry %d/%d in %ds", attempt, MAX_RETRIES - 1, wait)
                time.sleep(wait)
            try:
                resp = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = AdSourceError(f"Netwerkfout richting Meta API: {exc}")
                continue

            self._respect_usage_header(resp)

            if resp.status_code == 200:
                return resp.json()

            code, message = _parse_graph_error(resp)
            if code == ERROR_TOKEN:
                raise TokenError(TOKEN_HELP)
            if code in RATE_LIMIT_CODES or resp.status_code >= 500:
                last_error = AdSourceError(
                    f"Meta API tijdelijk niet beschikbaar (code {code}): {message}"
                )
                continue
            raise AdSourceError(f"Meta API-fout (code {code}): {message}")
        raise last_error or AdSourceError("Meta API bleef falen na retries")

    def _respect_usage_header(self, resp: httpx.Response) -> None:
        """Parse X-App-Usage and voluntarily slow down near the quota."""
        header = resp.headers.get("x-app-usage")
        if not header:
            return
        try:
            usage = json.loads(header)
            worst = max(
                usage.get("call_count", 0),
                usage.get("total_time", 0),
                usage.get("total_cputime", 0),
            )
        except (ValueError, TypeError):
            return
        if worst >= USAGE_SOFT_LIMIT:
            pause = 60 if worst >= 95 else 20
            logger.warning("App usage op %d%% — pauzeer %ds", worst, pause)
            time.sleep(pause)


def check_token(settings: Settings) -> tuple[bool, str]:
    """Lightweight token health check (used by `adscout status`)."""
    if not settings.meta_access_token:
        return False, "geen META_ACCESS_TOKEN in .env"
    try:
        resp = httpx.get(
            f"{settings.graph_base_url}/me",
            params={"access_token": settings.meta_access_token},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return False, f"netwerkfout richting Meta API: {exc}"
    if resp.status_code == 200:
        data = resp.json()
        who = data.get("name") or data.get("id") or "?"
        return True, f"token geldig (account: {who})"
    code, message = _parse_graph_error(resp)
    if code == ERROR_TOKEN:
        return False, TOKEN_HELP
    return False, f"Meta API-fout (code {code}): {message}"


def _parse_graph_error(resp: httpx.Response) -> tuple[int, str]:
    try:
        err = resp.json().get("error", {})
        return int(err.get("code", resp.status_code)), err.get("message", resp.text[:300])
    except (ValueError, TypeError):
        return resp.status_code, resp.text[:300]


def strip_access_token(url: str | None) -> str | None:
    """Remove the access_token query param from a snapshot URL.

    Meta embeds the caller's token in ad_snapshot_url; storing that would
    bake a live secret into the database (and any backup or data commit).
    The token is re-appended at request time where needed.
    """
    if not url:
        return url
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "access_token"]
    return urlunparse(parts._replace(query=urlencode(query)))


def parse_ad(item: dict[str, Any]) -> AdRecord:
    """Normalize one /ads_archive item into an AdRecord.

    ad_creative_bodies/titles/... are parallel arrays of text variants; they
    may differ in length, so zip to the longest and keep every variant.
    """
    bodies = item.get("ad_creative_bodies") or []
    titles = item.get("ad_creative_link_titles") or []
    captions = item.get("ad_creative_link_captions") or []
    descriptions = item.get("ad_creative_link_descriptions") or []
    n = max(len(bodies), len(titles), len(captions), len(descriptions), 0)
    texts = [
        AdTextVariant(
            body=bodies[i] if i < len(bodies) else None,
            title=titles[i] if i < len(titles) else None,
            caption=captions[i] if i < len(captions) else None,
            description=descriptions[i] if i < len(descriptions) else None,
        )
        for i in range(n)
    ]

    eu_reach = item.get("eu_total_reach")
    try:
        eu_reach = int(eu_reach) if eu_reach is not None else None
    except (TypeError, ValueError):
        eu_reach = None

    snapshot_url = strip_access_token(item.get("ad_snapshot_url"))
    if item.get("ad_snapshot_url"):
        item = {**item, "ad_snapshot_url": snapshot_url}  # keep raw token-free too

    return AdRecord(
        ad_archive_id=str(item.get("id", "")),
        page_id=str(item["page_id"]) if item.get("page_id") else None,
        page_name=item.get("page_name"),
        ad_creation_time=item.get("ad_creation_time"),
        ad_delivery_start=item.get("ad_delivery_start_time"),
        ad_delivery_stop=item.get("ad_delivery_stop_time"),
        snapshot_url=snapshot_url,
        platforms=list(item.get("publisher_platforms") or []),
        languages=list(item.get("languages") or []),
        texts=texts,
        eu_reach=eu_reach,
        raw=item,
    )
