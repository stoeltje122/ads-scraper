"""Read-side queries shared by dashboard, reports, CLI and export.

Runtime is the winner metric: runtime_days = (delivery_stop OR last_seen)
− delivery_start. For ads that vanished from the Ad Library without an
explicit stop date, last_seen is the honest end proxy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

# SQL fragment implementing the runtime metric (in days), clamped at 0
# like models.runtime_days.
RUNTIME_SQL = (
    "CAST(MAX(julianday(COALESCE(substr(ads.ad_delivery_stop, 1, 10), ads.last_seen)) "
    "- julianday(substr(ads.ad_delivery_start, 1, 10)), 0) AS INTEGER)"
)

# The day an ad (effectively) stopped: explicit stop date, else last sighting.
STOP_DAY_SQL = "COALESCE(substr(ads.ad_delivery_stop, 1, 10), ads.last_seen)"

_BASE_SELECT = f"""
SELECT ads.*,
       advertisers.name AS advertiser_name,
       advertisers.category AS advertiser_category,
       CASE WHEN ads.ad_delivery_start IS NOT NULL THEN {RUNTIME_SQL} END AS runtime_days,
       (SELECT body FROM ad_texts t WHERE t.ad_id = ads.ad_archive_id
        ORDER BY t.variant_index LIMIT 1) AS first_body,
       (SELECT local_path FROM creatives c WHERE c.ad_id = ads.ad_archive_id
        AND c.local_path IS NOT NULL ORDER BY c.id LIMIT 1) AS thumb_path,
       (SELECT COUNT(*) FROM ad_texts t WHERE t.ad_id = ads.ad_archive_id) AS n_variants
FROM ads
JOIN advertisers ON advertisers.id = ads.advertiser_id
"""


@dataclass
class AdFilter:
    advertiser_id: int | None = None
    advertiser_category: str | None = None
    tag_id: int | None = None
    format: str | None = None
    status: str | None = None            # active | inactive
    country: str | None = None
    min_runtime: int | None = None
    max_runtime: int | None = None
    first_seen_after: str | None = None  # ISO date
    first_seen_before: str | None = None
    search: str | None = None            # free text in ad copy
    sort: str = "newest"                 # newest | longest | recently_stopped
    limit: int = 60
    offset: int = 0


_SORTS = {
    "newest": "COALESCE(substr(ads.ad_delivery_start,1,10), ads.first_seen) DESC",
    "longest": "runtime_days DESC NULLS LAST",
    "recently_stopped": f"{STOP_DAY_SQL} DESC",
}


def _build_where(f: AdFilter) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if f.advertiser_id:
        where.append("ads.advertiser_id = ?")
        params.append(f.advertiser_id)
    if f.advertiser_category:
        where.append("advertisers.category = ?")
        params.append(f.advertiser_category)
    if f.tag_id:
        where.append(
            "EXISTS (SELECT 1 FROM ad_tags at WHERE at.ad_id = ads.ad_archive_id "
            "AND at.category_id = ? AND at.status = 'accepted')"
        )
        params.append(f.tag_id)
    if f.format:
        where.append("ads.format = ?")
        params.append(f.format)
    if f.status:
        where.append("ads.status = ?")
        params.append(f.status)
    if f.country:
        where.append("ads.countries LIKE ?")
        params.append(f'%"{f.country.upper()}"%')
    if f.min_runtime is not None:
        where.append(f"{RUNTIME_SQL} >= ?")
        params.append(f.min_runtime)
    if f.max_runtime is not None:
        where.append(f"{RUNTIME_SQL} <= ?")
        params.append(f.max_runtime)
    if f.first_seen_after:
        where.append("ads.first_seen >= ?")
        params.append(f.first_seen_after)
    if f.first_seen_before:
        where.append("ads.first_seen <= ?")
        params.append(f.first_seen_before)
    if f.search:
        where.append(
            "EXISTS (SELECT 1 FROM ad_texts t WHERE t.ad_id = ads.ad_archive_id "
            "AND (t.body LIKE ? OR t.title LIKE ?))"
        )
        like = f"%{f.search}%"
        params.extend([like, like])
    # "recent gestopt" without an explicit status filter implies stopped ads.
    if f.sort == "recently_stopped" and not f.status:
        where.append("ads.status = 'inactive'")
    return where, params


def query_ads(conn: sqlite3.Connection, f: AdFilter) -> list[sqlite3.Row]:
    where, params = _build_where(f)
    sql = _BASE_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {_SORTS.get(f.sort, _SORTS['newest'])} LIMIT ? OFFSET ?"
    params.extend([f.limit, f.offset])
    return conn.execute(sql, params).fetchall()


def count_ads(conn: sqlite3.Connection, f: AdFilter) -> int:
    where, params = _build_where(f)
    sql = "SELECT COUNT(*) FROM ads JOIN advertisers ON advertisers.id = ads.advertiser_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql, params).fetchone()[0]


def get_ad(conn: sqlite3.Connection, ad_id: str) -> sqlite3.Row | None:
    return conn.execute(
        _BASE_SELECT + " WHERE ads.ad_archive_id = ?", (ad_id,)
    ).fetchone()


def ad_texts(conn: sqlite3.Connection, ad_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ad_texts WHERE ad_id = ? ORDER BY variant_index", (ad_id,)
    ).fetchall()


def ad_creatives(conn: sqlite3.Connection, ad_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM creatives WHERE ad_id = ? ORDER BY id", (ad_id,)
    ).fetchall()


def ad_sightings(conn: sqlite3.Connection, ad_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seen_at, status, eu_reach FROM ad_snapshots WHERE ad_id = ? ORDER BY seen_at",
        (ad_id,),
    ).fetchall()


def ad_tags_for(conn: sqlite3.Connection, ad_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT at.*, c.name, c.tag_group FROM ad_tags at
           JOIN categories c ON c.id = at.category_id
           WHERE at.ad_id = ? ORDER BY c.tag_group, c.name""",
        (ad_id,),
    ).fetchall()


def shared_creative_ads(conn: sqlite3.Connection, ad_id: str) -> list[sqlite3.Row]:
    """Other ads using the same creative (matched on sha256) — reuse signal."""
    return conn.execute(
        """SELECT DISTINCT ads.ad_archive_id, advertisers.name AS advertiser_name
           FROM creatives c1
           JOIN creatives c2 ON c2.sha256 = c1.sha256 AND c2.ad_id != c1.ad_id
           JOIN ads ON ads.ad_archive_id = c2.ad_id
           JOIN advertisers ON advertisers.id = ads.advertiser_id
           WHERE c1.ad_id = ? AND c1.sha256 IS NOT NULL""",
        (ad_id,),
    ).fetchall()


# ── Winners / change detection ───────────────────────────────────────


def winners(
    conn: sqlite3.Connection, limit: int = 40, category: str | None = None
) -> list[sqlite3.Row]:
    """Longest-running *active* ads across all competitors."""
    f = AdFilter(status="active", sort="longest", limit=limit, advertiser_category=category)
    return query_ads(conn, f)


def new_since(conn: sqlite3.Connection, since: date, limit: int = 200) -> list[sqlite3.Row]:
    return query_ads(
        conn, AdFilter(first_seen_after=since.isoformat(), sort="newest", limit=limit)
    )


def stopped_since(conn: sqlite3.Connection, since: date, limit: int = 200) -> list[sqlite3.Row]:
    sql = _BASE_SELECT + f"""
        WHERE ads.status = 'inactive' AND {STOP_DAY_SQL} >= ?
        ORDER BY {STOP_DAY_SQL} DESC LIMIT ?"""
    return conn.execute(sql, (since.isoformat(), limit)).fetchall()


def volume_series(
    conn: sqlite3.Connection, advertiser_id: int, days: int = 90
) -> list[sqlite3.Row]:
    """Active-ad count per snapshot day for one advertiser (trend chart)."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return conn.execute(
        """SELECT s.seen_at, SUM(CASE WHEN s.status = 'active' THEN 1 ELSE 0 END) AS active
           FROM ad_snapshots s
           JOIN ads ON ads.ad_archive_id = s.ad_id
           WHERE ads.advertiser_id = ? AND s.seen_at >= ?
           GROUP BY s.seen_at ORDER BY s.seen_at""",
        (advertiser_id, since),
    ).fetchall()


def advertiser_week_stats(conn: sqlite3.Connection, since: date) -> list[sqlite3.Row]:
    """Per advertiser: current active count, new and stopped since `since`."""
    return conn.execute(
        f"""SELECT a.id, a.name, a.category, a.status AS advertiser_status,
               SUM(CASE WHEN ads.status = 'active' THEN 1 ELSE 0 END) AS active_now,
               SUM(CASE WHEN ads.first_seen >= :since THEN 1 ELSE 0 END) AS new_ads,
               SUM(CASE WHEN ads.status = 'inactive' AND {STOP_DAY_SQL} >= :since
                   THEN 1 ELSE 0 END) AS stopped_ads,
               COUNT(ads.ad_archive_id) AS total_ads
        FROM advertisers a
        LEFT JOIN ads ON ads.advertiser_id = a.id
        GROUP BY a.id ORDER BY a.name COLLATE NOCASE""",
        {"since": since.isoformat()},
    ).fetchall()


def last_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def pending_ai_suggestions(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT at.*, c.name AS tag_name, c.tag_group,
                  advertisers.name AS advertiser_name
           FROM ad_tags at
           JOIN categories c ON c.id = at.category_id
           JOIN ads ON ads.ad_archive_id = at.ad_id
           JOIN advertisers ON advertisers.id = ads.advertiser_id
           WHERE at.status = 'suggested'
           ORDER BY at.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def export_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Full flat export of ads incl. advertiser, runtime and accepted tags."""
    return conn.execute(
        _BASE_SELECT
        + """ ORDER BY advertisers.name COLLATE NOCASE,
                        COALESCE(substr(ads.ad_delivery_start,1,10), ads.first_seen) DESC"""
    ).fetchall()


def accepted_tags_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """SELECT at.ad_id, c.name FROM ad_tags at
           JOIN categories c ON c.id = at.category_id
           WHERE at.status = 'accepted' ORDER BY c.name"""
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["ad_id"], []).append(row["name"])
    return out
