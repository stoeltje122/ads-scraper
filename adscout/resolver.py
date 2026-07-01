"""Resolve brand names to Facebook page IDs — always human-confirmed.

Generic names (Ella, Oana) match many pages; the resolver only *presents*
candidates with evidence (name, page_id, ad counts, sample copy). Picking
is done by a person, never automatically.
"""

from __future__ import annotations

import logging
import sqlite3

from adscout import store
from adscout.models import PageCandidate
from adscout.sources.base import AdSource, AdSourceError

logger = logging.getLogger(__name__)

MANUAL_LOOKUP_HELP = """\
Geen kandidaten gevonden via de API. Handmatige route:
  1. Open https://www.facebook.com/ads/library/ in je browser.
  2. Kies land ({country}) en categorie 'Alle advertenties', zoek op "{brand}".
  3. Klik een advertentie van het juiste merk → klik op de paginanaam.
  4. Het page_id staat in de URL (of via 'Info en advertenties' van de pagina).
  5. Voeg toe met: adscout page add "{brand}" <page_id> --page-name "<naam>"
"""


def find_candidates(
    source: AdSource, brand: str, countries: list[str]
) -> list[PageCandidate]:
    """Search all configured countries and merge candidates per page_id."""
    merged: dict[str, PageCandidate] = {}
    for country in countries:
        try:
            for cand in source.search_pages(brand, country):
                existing = merged.get(cand.page_id)
                if existing:
                    existing.ads_seen += cand.ads_seen
                    existing.active_ads += cand.active_ads
                    existing.example_text = existing.example_text or cand.example_text
                else:
                    merged[cand.page_id] = cand
        except AdSourceError as exc:
            logger.error("Zoeken in %s faalde: %s", country, exc)
    return sorted(merged.values(), key=lambda c: (-c.active_ads, -c.ads_seen))


def confirm_page(
    conn: sqlite3.Connection,
    brand: str,
    candidate: PageCandidate,
    category: str | None = None,
    countries: list[str] | None = None,
) -> int:
    """Attach a *human-confirmed* candidate page to the advertiser."""
    adv = store.get_advertiser(conn, brand)
    advertiser_id = adv["id"] if adv else store.add_advertiser(
        conn, brand, category=category, countries=countries
    )
    store.add_page(conn, advertiser_id, candidate.page_id, candidate.page_name)
    logger.info(
        "Pagina bevestigd voor %s: %s (%s)", brand, candidate.page_name, candidate.page_id
    )
    return advertiser_id
