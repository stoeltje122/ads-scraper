"""Write-side storage: sources, competitors, themes, items, analyses.

Read-side queries for the dashboard/report live in queries.py. Every write
commits itself; idempotency comes from UNIQUE constraints + upserts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import yaml

from pulse import health
from pulse.models import AnalysisResult, FeedbackItem, author_hash, utc_now_iso

logger = logging.getLogger(__name__)

# The five standard channels. Manual import needs no credentials, so it is
# born active; gmail/meta wait for credentials. Trustpilot/bol start paused:
# verified (July 2026) that there is no legitimate automated route without a
# paid plan — their rows exist so manual imports can attach to the right
# channel, and so a future paid integration has a home.
STANDARD_SOURCES: list[tuple[str, str, str, str | None]] = [
    ("gmail", "Support-mail (Gmail)", "awaiting_config", None),
    ("meta_comments", "Facebook/Instagram-reacties", "awaiting_config", None),
    ("trustpilot", "Trustpilot-reviews", "paused",
     '{"route": "handmatig", "reden": "geen gratis API; zie PULSE.md"}'),
    ("bol", "bol-reviews", "paused",
     '{"route": "handmatig", "reden": "Retailer API ontsluit geen reviewtekst; zie PULSE.md"}'),
    ("manual", "Handmatige import", "active", None),
]

STATUS_LABELS_NL = {
    "active": "actief",
    "awaiting_config": "wacht op configuratie",
    "paused": "gepauzeerd",
}


# ── Seeding (idempotent; the database wins over the YAML) ────────────


def seed_sources(conn: sqlite3.Connection) -> int:
    n = 0
    for type_, name, status, config_json in STANDARD_SOURCES:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sources (type, name, status, config_json) VALUES (?, ?, ?, ?)",
            (type_, name, status, config_json),
        )
        n += cur.rowcount
    conn.commit()
    if n:
        logger.info("Bronnen geseed: %d nieuw", n)
    return n


def seed_themes(conn: sqlite3.Connection, taxonomy_path: Path | str) -> int:
    """Idempotent: only inserts unknown slugs, keeps founder edits intact."""
    data = yaml.safe_load(Path(taxonomy_path).read_text(encoding="utf-8")) or {}
    n = 0
    for theme in data.get("themes", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO themes (slug, description) VALUES (?, ?)",
            (theme["slug"], theme.get("description")),
        )
        n += cur.rowcount
    conn.commit()
    logger.info("Taxonomie geseed: %d thema's", n)
    return n


def seed_competitors(conn: sqlite3.Connection, seed_path: Path | str) -> int:
    """Seed the watchlist from competitors.seed.yaml (shared with AdScout).

    Accepts both the AdScout key (`advertisers`) and a `competitors` key.
    Existing names are left untouched; review URLs are for the founders to
    fill in (dashboard → Beheer, or `pulse competitor set-urls`).
    """
    data = yaml.safe_load(Path(seed_path).read_text(encoding="utf-8")) or {}
    entries = data.get("competitors", data.get("advertisers", []))
    n = 0
    for comp in entries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO competitors (name, category, notes) VALUES (?, ?, ?)",
            (comp["name"], comp.get("category"), comp.get("notes")),
        )
        n += cur.rowcount
    conn.commit()
    logger.info("Watchlist geseed: %d concurrenten", n)
    return n


# ── Sources ──────────────────────────────────────────────────────────


def get_source(conn: sqlite3.Connection, type_: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE type = ?", (type_,)).fetchone()


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # The i.id IS NOT NULL guard keeps a source with zero items from
    # counting its empty LEFT-JOIN row as one unanalyzed item.
    return conn.execute(
        """SELECT s.*, COUNT(i.id) AS n_items,
                  SUM(CASE WHEN i.id IS NOT NULL AND a.item_id IS NULL
                           THEN 1 ELSE 0 END) AS n_unanalyzed
           FROM sources s
           LEFT JOIN items i ON i.source_id = s.id
           LEFT JOIN analyses a ON a.item_id = i.id
           GROUP BY s.id ORDER BY s.id"""
    ).fetchall()


def set_source_status(conn: sqlite3.Connection, source_id: int, status: str) -> None:
    if status not in ("active", "awaiting_config", "paused"):
        raise ValueError(f"Onbekende bron-status: {status}")
    conn.execute("UPDATE sources SET status = ? WHERE id = ?", (status, source_id))
    conn.commit()


def touch_source_run(conn: sqlite3.Connection, source_id: int, at: str | None = None) -> None:
    conn.execute(
        "UPDATE sources SET last_run = ? WHERE id = ?", (at or utc_now_iso(), source_id)
    )
    conn.commit()


# ── Competitors ──────────────────────────────────────────────────────


def add_competitor(
    conn: sqlite3.Connection,
    name: str,
    category: str | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO competitors (name, category, notes) VALUES (?, ?, ?)",
        (name, category, notes),
    )
    conn.commit()
    return cur.lastrowid


def get_competitor(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Case-insensitive lookup so 'cloudpillo' in a CSV matches 'Cloudpillo'."""
    return conn.execute(
        "SELECT * FROM competitors WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def list_competitors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.*, COUNT(i.id) AS n_items
           FROM competitors c LEFT JOIN items i ON i.competitor_id = c.id
           GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()


def set_competitor_status(conn: sqlite3.Connection, competitor_id: int, status: str) -> None:
    if status not in ("active", "paused"):
        raise ValueError(f"Onbekende concurrent-status: {status}")
    conn.execute("UPDATE competitors SET status = ? WHERE id = ?", (status, competitor_id))
    conn.commit()


def update_competitor(
    conn: sqlite3.Connection,
    competitor_id: int,
    category: str | None = None,
    notes: str | None = None,
    review_urls: list[str] | None = None,
) -> None:
    """Only overwrite what was passed; None means 'leave as is'."""
    if category is not None:
        conn.execute("UPDATE competitors SET category = ? WHERE id = ?", (category, competitor_id))
    if notes is not None:
        conn.execute("UPDATE competitors SET notes = ? WHERE id = ?", (notes, competitor_id))
    if review_urls is not None:
        conn.execute(
            "UPDATE competitors SET review_urls_json = ? WHERE id = ?",
            (json.dumps(review_urls, ensure_ascii=False), competitor_id),
        )
    conn.commit()


# ── Themes ───────────────────────────────────────────────────────────


def list_themes(conn: sqlite3.Connection, include_archived: bool = False) -> list[sqlite3.Row]:
    if include_archived:
        return conn.execute("SELECT * FROM themes ORDER BY slug").fetchall()
    return conn.execute("SELECT * FROM themes WHERE status = 'active' ORDER BY slug").fetchall()


def add_theme(conn: sqlite3.Connection, slug: str, description: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO themes (slug, description) VALUES (?, ?)", (slug, description)
    )
    conn.commit()
    return cur.lastrowid


def set_theme_status(conn: sqlite3.Connection, theme_id: int, status: str) -> None:
    if status not in ("active", "archived"):
        raise ValueError(f"Onbekende thema-status: {status}")
    conn.execute("UPDATE themes SET status = ? WHERE id = ?", (status, theme_id))
    conn.commit()


def update_theme_description(conn: sqlite3.Connection, theme_id: int, description: str) -> None:
    conn.execute("UPDATE themes SET description = ? WHERE id = ?", (description, theme_id))
    conn.commit()


# ── Items ────────────────────────────────────────────────────────────


def upsert_thread(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
    subject: str | None,
    message_at: str | None,
) -> int:
    """One thread row per mail conversation; last_message_at moves forward."""
    conn.execute(
        """INSERT INTO threads (source_id, external_id, subject, first_seen, last_message_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_id, external_id) DO UPDATE SET
             subject = COALESCE(threads.subject, excluded.subject),
             last_message_at = MAX(COALESCE(threads.last_message_at, ''),
                                   COALESCE(excluded.last_message_at, ''))""",
        (source_id, external_id, subject, utc_now_iso(), message_at),
    )
    row = conn.execute(
        "SELECT id FROM threads WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    return row["id"]


def store_items(
    conn: sqlite3.Connection, source_id: int, items: list[FeedbackItem]
) -> tuple[int, int]:
    """Insert new items, skip known ones. Returns (seen, new).

    Idempotent on (source_id, external_id). The author's raw identifier is
    hashed here and never stored; unknown competitor names are created on
    the fly so a manual import can never silently lose the competitor link.
    """
    seen = new = 0
    now = utc_now_iso()
    for item in items:
        seen += 1
        competitor_id = None
        if item.competitor_name:
            comp = get_competitor(conn, item.competitor_name)
            if comp is None:
                competitor_id = add_competitor(conn, item.competitor_name.strip())
                logger.info("Nieuwe concurrent aangemaakt uit import: %s", item.competitor_name)
            else:
                competitor_id = comp["id"]

        thread_id = None
        if item.thread_external_id:
            thread_id = upsert_thread(
                conn, source_id, item.thread_external_id, item.thread_subject, item.happened_at
            )

        cur = conn.execute(
            """INSERT OR IGNORE INTO items
               (source_id, competitor_id, external_id, happened_at, language,
                author_display, author_hash, text, url, thread_id, first_seen,
                raw_json, pre_health_flag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                competitor_id,
                item.external_id,
                item.happened_at,
                item.language,
                item.author_display,
                author_hash(item.author_ref),
                item.text,
                item.url,
                thread_id,
                now,
                item.raw_json() if item.raw else None,
                1 if health.health_screen(item.text) else 0,
            ),
        )
        new += cur.rowcount
    conn.commit()
    return seen, new


def mark_followed_up(conn: sqlite3.Connection, item_id: int, done: bool = True) -> None:
    conn.execute(
        "UPDATE items SET followed_up_at = ? WHERE id = ?",
        (utc_now_iso() if done else None, item_id),
    )
    conn.commit()


def mark_thread_seen(conn: sqlite3.Connection, thread_id: int) -> None:
    conn.execute("UPDATE threads SET status = 'seen' WHERE id = ?", (thread_id,))
    conn.commit()


# ── Analyses ─────────────────────────────────────────────────────────


def save_analysis(
    conn: sqlite3.Connection, item_id: int, result: AnalysisResult, model: str
) -> None:
    """Store one analysis (replacing any earlier one) and clear the error
    marker. health_flag forces urgency to 'urgent' — enforced here so no
    prompt regression can ever soften it."""
    urgency = "urgent" if result.health_flag else result.urgency
    conn.execute(
        """INSERT INTO analyses
           (item_id, sentiment, themes_json, urgency, type, health_flag,
            competitor_pro, competitor_con, confidence, model, analyzed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
             sentiment = excluded.sentiment, themes_json = excluded.themes_json,
             urgency = excluded.urgency, type = excluded.type,
             health_flag = excluded.health_flag,
             competitor_pro = excluded.competitor_pro,
             competitor_con = excluded.competitor_con,
             confidence = excluded.confidence, model = excluded.model,
             analyzed_at = excluded.analyzed_at""",
        (
            item_id,
            result.sentiment,
            json.dumps(result.themes, ensure_ascii=False),
            urgency,
            result.type,
            1 if result.health_flag else 0,
            result.competitor_pro,
            result.competitor_con,
            result.confidence,
            model,
            utc_now_iso(),
        ),
    )
    if result.language:
        conn.execute(
            "UPDATE items SET language = COALESCE(language, ?) WHERE id = ?",
            (result.language, item_id),
        )
    conn.execute(
        "UPDATE items SET analysis_error = NULL WHERE id = ?", (item_id,)
    )
    conn.commit()


def mark_analysis_failure(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    """Parse/API failure: keep the item in the queue, visibly, never drop it."""
    conn.execute(
        """UPDATE items SET analysis_attempts = analysis_attempts + 1,
                            analysis_error = ? WHERE id = ?""",
        (error[:500], item_id),
    )
    conn.commit()


# ── Privacy: forget & retention ──────────────────────────────────────


def _drop_orphan_threads(conn: sqlite3.Connection) -> None:
    """Threads whose items are all gone must go too: a thread subject can
    itself contain a person's name (AVG)."""
    conn.execute(
        "DELETE FROM threads WHERE id NOT IN "
        "(SELECT DISTINCT thread_id FROM items WHERE thread_id IS NOT NULL)"
    )


def forget(conn: sqlite3.Connection, hash_or_email: str) -> int:
    """Delete every item of one person (AVG). Accepts the author-hash as
    shown in the dashboard, or the raw e-mail address (hashed on the spot,
    not stored). Analyses cascade via the foreign key; mail threads that
    end up empty are removed too (their subject can contain the name)."""
    value = hash_or_email.strip()
    candidates = {value.casefold(), author_hash(value)}
    placeholders = ",".join("?" for _ in candidates)
    cur = conn.execute(
        f"DELETE FROM items WHERE author_hash IN ({placeholders})",
        tuple(candidates),
    )
    _drop_orphan_threads(conn)
    conn.commit()
    logger.info("Vergeten: %d items verwijderd", cur.rowcount)
    return cur.rowcount


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    conn.commit()


def retention_months(conn: sqlite3.Connection) -> int:
    from pulse.config import DEFAULT_RETENTION_MONTHS

    try:
        months = int(get_setting(conn, "retention_months", str(DEFAULT_RETENTION_MONTHS)))
    except ValueError:
        return DEFAULT_RETENTION_MONTHS
    return months if months > 0 else DEFAULT_RETENTION_MONTHS


def retention_cleanup(conn: sqlite3.Connection, now_iso: str | None = None) -> int:
    """Delete items older than the configured retention (default 24 months).

    Age is based on when the feedback was written (happened_at), falling
    back to first_seen. Runs at the end of every collect. Approximates a
    month as 30.44 days — precise enough for a retention policy.
    """
    months = retention_months(conn)
    now = now_iso or utc_now_iso()
    cutoff_expr = f"-{int(months * 30.44)} days"
    # Compare calendar days, not raw strings: stored timestamps use 'T' as
    # separator while sqlite's datetime() emits a space, which would skew a
    # lexicographic comparison on the cutoff day itself.
    cur = conn.execute(
        """DELETE FROM items
           WHERE date(COALESCE(happened_at, first_seen)) < date(datetime(?, ?))""",
        (now, cutoff_expr),
    )
    _drop_orphan_threads(conn)
    conn.commit()
    if cur.rowcount:
        logger.info("Retentie-opschoning: %d items ouder dan %d maanden verwijderd", cur.rowcount, months)
    return cur.rowcount


# ── Runs (audit trail) ───────────────────────────────────────────────


def record_run(
    conn: sqlite3.Connection,
    kind: str,
    started_at: str,
    finished_at: str,
    ok: bool,
    items_seen: int = 0,
    items_new: int = 0,
    items_analyzed: int = 0,
    items_deleted: int = 0,
    errors: list[str] | None = None,
    detail: list[dict] | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO runs (kind, started_at, finished_at, ok, items_seen,
                             items_new, items_analyzed, items_deleted, errors, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kind,
            started_at,
            finished_at,
            1 if ok else 0,
            items_seen,
            items_new,
            items_analyzed,
            items_deleted,
            json.dumps(errors or [], ensure_ascii=False),
            json.dumps(detail or [], ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid
