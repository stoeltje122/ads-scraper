"""The daily collect run.

Guarantees:
- Idempotent: running twice on one day creates no duplicates
  (ads upsert on ad_archive_id, snapshots unique on (ad_id, seen_at)).
- One failing advertiser never blocks the rest: errors are logged into the
  run record and the loop continues.
- Ads that vanish from the Ad Library keep their history here — that is the
  whole point of AdScout.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from adscout.models import AdRecord
from adscout.sources.base import AdSource, AdSourceError, TokenError

logger = logging.getLogger(__name__)


@dataclass
class AdvertiserResult:
    advertiser: str
    ads_fetched: int = 0
    new_ads: int = 0
    stopped_ads: int = 0
    error: str | None = None
    skipped: str | None = None


@dataclass
class RunResult:
    run_id: int | None = None
    started_at: str = ""
    finished_at: str = ""
    per_advertiser: list[AdvertiserResult] = field(default_factory=list)
    new_ad_ids: list[str] = field(default_factory=list)
    token_error: str | None = None

    @property
    def ads_fetched(self) -> int:
        return sum(r.ads_fetched for r in self.per_advertiser)

    @property
    def new_ads(self) -> int:
        return sum(r.new_ads for r in self.per_advertiser)

    @property
    def stopped_ads(self) -> int:
        return sum(r.stopped_ads for r in self.per_advertiser)

    @property
    def errors(self) -> list[str]:
        return [f"{r.advertiser}: {r.error}" for r in self.per_advertiser if r.error]

    @property
    def ok(self) -> bool:
        return not self.errors


def collect(
    conn: sqlite3.Connection,
    source: AdSource,
    run_date: date | None = None,
) -> RunResult:
    """Run one collect pass over all active advertisers."""
    run_date = run_date or datetime.now(timezone.utc).date()
    result = RunResult(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    advertisers = conn.execute(
        "SELECT * FROM advertisers WHERE status = 'active' ORDER BY name"
    ).fetchall()

    for adv in advertisers:
        adv_result = AdvertiserResult(advertiser=adv["name"])
        result.per_advertiser.append(adv_result)

        pages = [
            row["page_id"]
            for row in conn.execute(
                "SELECT page_id FROM advertiser_pages WHERE advertiser_id = ?", (adv["id"],)
            )
        ]
        if not pages:
            adv_result.skipped = "geen page_id — draai eerst `adscout resolve`"
            logger.info("Sla %s over: %s", adv["name"], adv_result.skipped)
            continue

        countries = [c.strip().upper() for c in (adv["countries"] or "NL").split(",") if c.strip()]
        seen_ids: set[str] = set()
        fetch_complete = True
        try:
            for country in countries:
                for record in source.fetch_ads(pages, country, active_status="ALL"):
                    if not record.ad_archive_id:
                        continue
                    is_new, just_stopped = upsert_ad(conn, adv["id"], record, country, run_date)
                    adv_result.ads_fetched += 1
                    if record.ad_archive_id not in seen_ids:
                        seen_ids.add(record.ad_archive_id)
                        adv_result.new_ads += 1 if is_new else 0
                        adv_result.stopped_ads += 1 if just_stopped else 0
                        if is_new:
                            result.new_ad_ids.append(record.ad_archive_id)
            conn.commit()
        except TokenError as exc:
            # A broken token affects every advertiser: stop fetching, but do
            # record the run so the audit trail shows what happened.
            conn.commit()
            adv_result.error = str(exc)
            result.token_error = str(exc)
            logger.error("Token-probleem — run afgebroken: %s", exc)
            break
        except AdSourceError as exc:
            conn.commit()
            fetch_complete = False
            adv_result.error = str(exc)
            logger.error("Concurrent %s faalde: %s — run gaat door", adv["name"], exc)
        except Exception as exc:  # defensive: any bug in parsing one advertiser
            conn.commit()
            fetch_complete = False
            adv_result.error = f"onverwachte fout: {exc}"
            logger.exception("Onverwachte fout bij %s — run gaat door", adv["name"])

        # Vanished ads: previously active but no longer returned at all.
        # Only when this advertiser's fetch completed — a partial fetch must
        # never mark ads as stopped. An empty result set is treated as an
        # anomaly too: with ad_active_status=ALL even stopped ads keep
        # appearing, so "suddenly nothing at all" means API trouble, not a
        # brand that stopped advertising.
        if fetch_complete and seen_ids:
            adv_result.stopped_ads += mark_vanished(conn, adv["id"], seen_ids, run_date)
        elif fetch_complete and not seen_ids:
            logger.warning(
                "%s: 0 ads teruggekregen — vanish-detectie overgeslagen", adv["name"]
            )

    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.run_id = record_run(conn, result)
    return result


def upsert_ad(
    conn: sqlite3.Connection,
    advertiser_id: int,
    record: AdRecord,
    country: str,
    run_date: date,
) -> tuple[bool, bool]:
    """Insert or update one ad + its texts + today's snapshot.

    Returns (is_new, just_stopped).
    """
    day = run_date.isoformat()
    status = "active" if record.is_active(run_date) else "inactive"

    existing = conn.execute(
        "SELECT status, countries FROM ads WHERE ad_archive_id = ?", (record.ad_archive_id,)
    ).fetchone()

    is_new = existing is None
    just_stopped = bool(existing and existing["status"] == "active" and status == "inactive")

    if is_new:
        conn.execute(
            """INSERT INTO ads (ad_archive_id, advertiser_id, page_id, first_seen, last_seen,
                                ad_creation_time, ad_delivery_start, ad_delivery_stop, status,
                                snapshot_url, landing_url, platforms, languages, countries,
                                eu_reach_latest, raw_json_latest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.ad_archive_id,
                advertiser_id,
                record.page_id,
                day,
                day,
                record.ad_creation_time,
                record.ad_delivery_start,
                record.ad_delivery_stop,
                status,
                record.snapshot_url,
                _landing_hint(record),
                json.dumps(record.platforms),
                json.dumps(record.languages),
                json.dumps([country]),
                record.eu_reach,
                record.raw_json(),
            ),
        )
    else:
        countries = set(json.loads(existing["countries"] or "[]"))
        countries.add(country)
        conn.execute(
            """UPDATE ads SET last_seen = ?, ad_delivery_start = ?, ad_delivery_stop = ?,
                              status = ?, snapshot_url = ?, platforms = ?, languages = ?,
                              countries = ?, eu_reach_latest = ?, raw_json_latest = ?
               WHERE ad_archive_id = ?""",
            (
                day,
                record.ad_delivery_start,
                record.ad_delivery_stop,
                status,
                record.snapshot_url,
                json.dumps(record.platforms),
                json.dumps(record.languages),
                json.dumps(sorted(countries)),
                record.eu_reach,
                record.raw_json(),
                record.ad_archive_id,
            ),
        )

    for i, text in enumerate(record.texts):
        conn.execute(
            """INSERT INTO ad_texts (ad_id, variant_index, body, title, caption, description)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ad_id, variant_index) DO UPDATE SET
                 body = excluded.body, title = excluded.title,
                 caption = excluded.caption, description = excluded.description""",
            (record.ad_archive_id, i, text.body, text.title, text.caption, text.description),
        )

    # One snapshot per ad per day → idempotent second run.
    conn.execute(
        """INSERT INTO ad_snapshots (ad_id, seen_at, status, eu_reach, raw_json)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(ad_id, seen_at) DO UPDATE SET
             status = excluded.status, eu_reach = excluded.eu_reach,
             raw_json = excluded.raw_json""",
        (record.ad_archive_id, day, status, record.eu_reach, record.raw_json()),
    )
    return is_new, just_stopped


def mark_vanished(
    conn: sqlite3.Connection, advertiser_id: int, seen_ids: set[str], run_date: date
) -> int:
    """Mark previously-active ads that no longer appear in the API as inactive.

    Their last_seen stays at the last real sighting — that is our honest
    end-date proxy for runtime.
    """
    rows = conn.execute(
        "SELECT ad_archive_id FROM ads WHERE advertiser_id = ? AND status = 'active'",
        (advertiser_id,),
    ).fetchall()
    vanished = [r["ad_archive_id"] for r in rows if r["ad_archive_id"] not in seen_ids]
    for ad_id in vanished:
        conn.execute("UPDATE ads SET status = 'inactive' WHERE ad_archive_id = ?", (ad_id,))
        logger.info("Ad %s niet meer zichtbaar in Ad Library → inactief", ad_id)
    conn.commit()
    return len(vanished)


def record_run(conn: sqlite3.Connection, result: RunResult) -> int:
    detail = [
        {
            "advertiser": r.advertiser,
            "ads_fetched": r.ads_fetched,
            "new_ads": r.new_ads,
            "stopped_ads": r.stopped_ads,
            "error": r.error,
            "skipped": r.skipped,
        }
        for r in result.per_advertiser
    ]
    cur = conn.execute(
        """INSERT INTO runs (started_at, finished_at, ok, ads_fetched, new_ads,
                             stopped_ads, errors, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result.started_at,
            result.finished_at,
            1 if result.ok else 0,
            result.ads_fetched,
            result.new_ads,
            result.stopped_ads,
            json.dumps(result.errors, ensure_ascii=False),
            json.dumps(detail, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _landing_hint(record: AdRecord) -> str | None:
    """Best available landing hint: the link caption usually holds the domain."""
    for text in record.texts:
        if text.caption:
            return text.caption
    return None
