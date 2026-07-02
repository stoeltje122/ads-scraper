"""Read-side queries shared by dashboard, report, CLI and export.

Write-side lives in store.py. Everything here returns sqlite3.Row lists
(or plain dicts for aggregates) and never mutates.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

# One JOIN block used by every item listing so columns never drift.
_ITEM_SELECT = """
    SELECT i.*, s.name AS source_name, s.type AS source_type,
           c.name AS competitor_name,
           a.sentiment, a.themes_json, a.urgency, a.type AS item_type,
           a.health_flag, a.competitor_pro, a.competitor_con,
           a.confidence, a.model, a.analyzed_at,
           t.status AS thread_status, t.subject AS thread_subject
    FROM items i
    JOIN sources s ON s.id = i.source_id
    LEFT JOIN competitors c ON c.id = i.competitor_id
    LEFT JOIN analyses a ON a.item_id = i.id
    LEFT JOIN threads t ON t.id = i.thread_id
"""


@dataclass
class ItemFilter:
    """Inbox filters; every field is optional and combines with AND."""

    source_id: int | None = None
    sentiment: str | None = None
    theme: str | None = None
    type_: str | None = None
    urgency: str | None = None
    about: str | None = None          # 'own' | 'competitor' | str(competitor_id)
    q: str | None = None
    days: int | None = None           # last N days (on happened_at/first_seen)
    health_only: bool = False
    unanalyzed_only: bool = False

    def where(self) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if self.source_id:
            clauses.append("i.source_id = ?")
            params.append(self.source_id)
        if self.sentiment:
            clauses.append("a.sentiment = ?")
            params.append(self.sentiment)
        if self.theme:
            # themes_json is a JSON array of slugs; LIKE on the quoted slug
            # is exact enough (slugs contain no quotes) and needs no JSON1.
            clauses.append("a.themes_json LIKE ?")
            params.append(f'%"{self.theme}"%')
        if self.type_:
            clauses.append("a.type = ?")
            params.append(self.type_)
        if self.urgency:
            clauses.append("a.urgency = ?")
            params.append(self.urgency)
        if self.about == "own":
            clauses.append("i.competitor_id IS NULL")
        elif self.about == "competitor":
            clauses.append("i.competitor_id IS NOT NULL")
        elif self.about and self.about.isdigit():
            clauses.append("i.competitor_id = ?")
            params.append(int(self.about))
        if self.q:
            clauses.append("(i.text LIKE ? OR i.author_display LIKE ?)")
            like = f"%{self.q}%"
            params.extend([like, like])
        if self.days:
            clauses.append("COALESCE(i.happened_at, i.first_seen) >= date('now', ?)")
            params.append(f"-{int(self.days)} days")
        if self.health_only:
            clauses.append("(a.health_flag = 1 OR (a.item_id IS NULL AND i.pre_health_flag = 1))")
        if self.unanalyzed_only:
            clauses.append("a.item_id IS NULL")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_items(
    conn: sqlite3.Connection, flt: ItemFilter, limit: int = 50, offset: int = 0
) -> list[sqlite3.Row]:
    where, params = flt.where()
    return conn.execute(
        _ITEM_SELECT + where +
        " ORDER BY COALESCE(i.happened_at, i.first_seen) DESC, i.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_items(conn: sqlite3.Connection, flt: ItemFilter) -> int:
    # Filters only touch i.* and a.* columns, so counting skips the other joins.
    where, params = flt.where()
    return conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN analyses a ON a.item_id = i.id " + where,
        params,
    ).fetchone()[0]


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(_ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()


def thread_items(conn: sqlite3.Connection, thread_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        _ITEM_SELECT + " WHERE i.thread_id = ? ORDER BY COALESCE(i.happened_at, i.first_seen)",
        (thread_id,),
    ).fetchall()


# ── Urgent view ──────────────────────────────────────────────────────


def urgent_items(conn: sqlite3.Connection, include_followed_up: bool = False) -> list[sqlite3.Row]:
    """Everything needing human eyes: analyzed urgent/health items, plus
    keyword-pre-flagged items still waiting for analysis. Health first.
    Competitor items are excluded on purpose — a side effect at another
    brand is a Kansen insight, not one of our support cases."""
    followed = "" if include_followed_up else "AND i.followed_up_at IS NULL"
    return conn.execute(
        _ITEM_SELECT
        + f"""WHERE (a.urgency = 'urgent' OR a.health_flag = 1
                     OR (a.item_id IS NULL AND i.pre_health_flag = 1))
              AND i.competitor_id IS NULL
              {followed}
           ORDER BY COALESCE(a.health_flag, i.pre_health_flag) DESC,
                    COALESCE(i.happened_at, i.first_seen) DESC"""
    ).fetchall()


def count_urgent_open(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM items i LEFT JOIN analyses a ON a.item_id = i.id
           WHERE (a.urgency = 'urgent' OR a.health_flag = 1
                  OR (a.item_id IS NULL AND i.pre_health_flag = 1))
             AND i.competitor_id IS NULL
             AND i.followed_up_at IS NULL"""
    ).fetchone()[0]


# ── Trends ───────────────────────────────────────────────────────────


def theme_counts(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    only_own: bool = False,
    sentiment: str | None = None,
    type_: str | None = None,
) -> list[dict]:
    """Counts per theme slug over a period, descending."""
    clauses, params = ["1=1"], []
    if start:
        clauses.append("COALESCE(i.happened_at, i.first_seen) >= ?")
        params.append(start)
    if end:
        clauses.append("COALESCE(i.happened_at, i.first_seen) < ?")
        params.append(end)
    if only_own:
        clauses.append("i.competitor_id IS NULL")
    if sentiment:
        clauses.append("a.sentiment = ?")
        params.append(sentiment)
    if type_:
        clauses.append("a.type = ?")
        params.append(type_)
    rows = conn.execute(
        f"""SELECT a.themes_json FROM analyses a JOIN items i ON i.id = a.item_id
            WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for slug in json.loads(row["themes_json"] or "[]"):
            counts[slug] = counts.get(slug, 0) + 1
    return [
        {"theme": slug, "count": n}
        for slug, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def sentiment_per_week(conn: sqlite3.Connection, weeks: int = 12) -> list[dict]:
    """Per ISO week: counts per sentiment (analyzed, own-brand items only)."""
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    rows = conn.execute(
        """SELECT strftime('%Y-W%W', COALESCE(i.happened_at, i.first_seen)) AS week,
                  a.sentiment, COUNT(*) AS n
           FROM analyses a JOIN items i ON i.id = a.item_id
           WHERE COALESCE(i.happened_at, i.first_seen) >= ?
             AND i.competitor_id IS NULL
           GROUP BY week, a.sentiment ORDER BY week""",
        (since,),
    ).fetchall()
    weeks_map: dict[str, dict] = {}
    for row in rows:
        bucket = weeks_map.setdefault(
            row["week"], {"week": row["week"], "positive": 0, "neutral": 0, "negative": 0}
        )
        bucket[row["sentiment"]] = row["n"]
    return list(weeks_map.values())


def items_per_week(conn: sqlite3.Connection, weeks: int = 12) -> list[dict]:
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    rows = conn.execute(
        """SELECT strftime('%Y-W%W', COALESCE(happened_at, first_seen)) AS week,
                  COUNT(*) AS n
           FROM items WHERE COALESCE(happened_at, first_seen) >= ?
           GROUP BY week ORDER BY week""",
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def week_over_week(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Top themes this week vs the 7 days before, for the Trends page and
    the weekly report."""
    today = today or date.today()
    this_start = (today - timedelta(days=7)).isoformat()
    prev_start = (today - timedelta(days=14)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    def by_type(type_: str, start: str, stop: str) -> list[dict]:
        return theme_counts(conn, start=start, end=stop, only_own=True, type_=type_)[:3]

    return {
        "complaints_now": by_type("complaint", this_start, end),
        "complaints_prev": by_type("complaint", prev_start, this_start),
        "compliments_now": by_type("compliment", this_start, end),
        "compliments_prev": by_type("compliment", prev_start, this_start),
        "themes_now": theme_counts(conn, start=this_start, end=end, only_own=True),
        "themes_prev": theme_counts(conn, start=prev_start, end=this_start, only_own=True),
    }


# ── Competitor kansen ────────────────────────────────────────────────


def competitor_insights(conn: sqlite3.Connection) -> list[dict]:
    """Per competitor: item count, sentiment split, recurring cons/pros.
    Feeds the Kansen page: 'bij merk X klagen mensen structureel over Y'."""
    out: list[dict] = []
    for comp in conn.execute(
        "SELECT * FROM competitors ORDER BY name COLLATE NOCASE"
    ).fetchall():
        rows = conn.execute(
            """SELECT a.sentiment, a.themes_json, a.competitor_pro, a.competitor_con
               FROM analyses a JOIN items i ON i.id = a.item_id
               WHERE i.competitor_id = ?""",
            (comp["id"],),
        ).fetchall()
        n_items = conn.execute(
            "SELECT COUNT(*) FROM items WHERE competitor_id = ?", (comp["id"],)
        ).fetchone()[0]
        themes: dict[str, int] = {}
        cons: list[str] = []
        pros: list[str] = []
        sentiments = {"positive": 0, "neutral": 0, "negative": 0}
        for row in rows:
            sentiments[row["sentiment"]] = sentiments.get(row["sentiment"], 0) + 1
            for slug in json.loads(row["themes_json"] or "[]"):
                themes[slug] = themes.get(slug, 0) + 1
            if row["competitor_con"]:
                cons.append(row["competitor_con"])
            if row["competitor_pro"]:
                pros.append(row["competitor_pro"])
        out.append(
            {
                "competitor": comp,
                "n_items": n_items,
                "n_analyzed": len(rows),
                "sentiments": sentiments,
                "top_themes": sorted(themes.items(), key=lambda kv: -kv[1])[:5],
                "cons": cons,
                "pros": pros,
                "review_urls": json.loads(comp["review_urls_json"] or "[]"),
            }
        )
    return out


# ── Status / runs / export ───────────────────────────────────────────


def last_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def failed_analyses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Items marked for re-analysis (never silently dropped)."""
    return conn.execute(
        """SELECT i.id, i.analysis_attempts, i.analysis_error, i.text
           FROM items i LEFT JOIN analyses a ON a.item_id = i.id
           WHERE a.item_id IS NULL AND i.analysis_attempts > 0
           ORDER BY i.analysis_attempts DESC, i.id"""
    ).fetchall()


def export_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        _ITEM_SELECT + " ORDER BY COALESCE(i.happened_at, i.first_seen) DESC, i.id DESC"
    ).fetchall()


def counts_summary(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
    return {
        "items": total,
        "analyzed": analyzed,
        "queue": total - analyzed,
        "urgent_open": count_urgent_open(conn),
        "failed": len(failed_analyses(conn)),
    }
