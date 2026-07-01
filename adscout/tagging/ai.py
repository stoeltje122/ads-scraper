"""Optional AI tag suggestions via the Claude API.

Strictly optional: AdScout works fully without ANTHROPIC_API_KEY. With a
key, new ads get tag *suggestions* (source='ai', status='suggested') that
a human accepts or rejects in the dashboard. AI never sets accepted tags.

The `anthropic` package is imported lazily so it is not a hard dependency
(install via requirements-ai.txt).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from adscout import store
from adscout.config import Settings

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2500

SYSTEM_PROMPT = """\
Je bent een advertentie-analist voor een Nederlands slaapsupplementmerk.
Je krijgt de tekst en metadata van één Meta-advertentie van een concurrent,
plus een vaste lijst toegestane tags. Kies uitsluitend tags uit die lijst
die duidelijk van toepassing zijn (meestal 2 tot 6 stuks).

Antwoord met ALLEEN een JSON-array, geen andere tekst:
[{"tag": "<exacte tagnaam uit de lijst>", "confidence": 0.0-1.0}]
"""


def suggest_for_untagged(
    conn: sqlite3.Connection, settings: Settings, limit: int = 25
) -> int:
    """Suggest tags for ads that have no tags/suggestions yet. Returns #ads tagged."""
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "Geen ANTHROPIC_API_KEY in .env — AI-suggesties staan uit. "
            "AdScout werkt verder volledig zonder."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Het 'anthropic'-pakket is niet geïnstalleerd. "
            "Installeer met: pip install -r requirements-ai.txt"
        ) from exc

    tags = store.list_categories(conn, "ad_tag")
    if not tags:
        raise RuntimeError("Geen ad-tags in de database — draai eerst `adscout init`.")
    tag_by_name = {t["name"]: t["id"] for t in tags}
    tag_list = "\n".join(f"- {t['name']}: {t['description'] or ''}" for t in tags)

    ads = conn.execute(
        """SELECT ads.ad_archive_id, ads.format, advertisers.name AS advertiser_name,
                  advertisers.category AS advertiser_category
           FROM ads
           JOIN advertisers ON advertisers.id = ads.advertiser_id
           WHERE NOT EXISTS (SELECT 1 FROM ad_tags at WHERE at.ad_id = ads.ad_archive_id)
             AND EXISTS (SELECT 1 FROM ad_texts t WHERE t.ad_id = ads.ad_archive_id
                         AND t.body IS NOT NULL)
           ORDER BY ads.first_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not ads:
        logger.info("Geen ongetagde ads met tekst gevonden.")
        return 0

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    tagged = 0
    for ad in ads:
        texts = conn.execute(
            "SELECT body, title FROM ad_texts WHERE ad_id = ? ORDER BY variant_index LIMIT 3",
            (ad["ad_archive_id"],),
        ).fetchall()
        copy = "\n---\n".join(
            f"{t['title'] or ''}\n{t['body'] or ''}".strip() for t in texts
        )[:MAX_TEXT_CHARS]

        user_msg = (
            f"Toegestane tags:\n{tag_list}\n\n"
            f"Advertentie van: {ad['advertiser_name']} "
            f"(categorie: {ad['advertiser_category'] or 'onbekend'})\n"
            f"Format: {ad['format']}\n\nTekst:\n{copy}"
        )
        try:
            resp = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            suggestions = _parse_suggestions(raw)
        except Exception as exc:
            logger.error("AI-suggestie voor ad %s mislukt: %s", ad["ad_archive_id"], exc)
            continue

        n = 0
        for tag_name, confidence in suggestions:
            category_id = tag_by_name.get(tag_name)
            if category_id is None:
                continue  # model invented a tag → drop silently
            store.suggest_tag(conn, ad["ad_archive_id"], category_id, confidence)
            n += 1
        if n:
            tagged += 1
            logger.info("Ad %s: %d tag-suggesties", ad["ad_archive_id"], n)
    return tagged


def _parse_suggestions(raw: str) -> list[tuple[str, float]]:
    """Lenient parse: find the first JSON array in the reply."""
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    out: list[tuple[str, float]] = []
    for item in items:
        if not isinstance(item, dict) or "tag" not in item:
            continue
        try:
            conf = min(max(float(item.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = 0.5
        out.append((str(item["tag"]), conf))
    return out
