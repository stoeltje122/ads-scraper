"""Write-side storage: watchlist, categories, tags, YAML seeding."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# ── Advertisers (watchlist) ──────────────────────────────────────────


def add_advertiser(
    conn: sqlite3.Connection,
    name: str,
    category: str | None = None,
    countries: list[str] | None = None,
    notes: str | None = None,
) -> int:
    """Insert, or update only the fields that were actually provided.

    Deliberately select-then-write instead of INSERT..ON CONFLICT:
    lastrowid is unreliable on the conflict path, and a re-add must never
    silently reset fields (like countries) that weren't passed.
    """
    existing = conn.execute("SELECT id FROM advertisers WHERE name = ?", (name,)).fetchone()
    if existing:
        update_advertiser(conn, existing["id"], category=category, countries=countries, notes=notes)
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO advertisers (name, category, countries, notes) VALUES (?, ?, ?, ?)",
        (name, category, ",".join(countries or ["NL"]), notes),
    )
    conn.commit()
    return cur.lastrowid


def get_advertiser(conn: sqlite3.Connection, name_or_id: str) -> sqlite3.Row | None:
    if str(name_or_id).isdigit():
        row = conn.execute(
            "SELECT * FROM advertisers WHERE id = ?", (int(name_or_id),)
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM advertisers WHERE name = ? COLLATE NOCASE", (str(name_or_id),)
    ).fetchone()


def list_advertisers(conn: sqlite3.Connection, include_paused: bool = True) -> list[sqlite3.Row]:
    sql = """SELECT a.*,
                    (SELECT COUNT(*) FROM advertiser_pages p WHERE p.advertiser_id = a.id) AS n_pages,
                    (SELECT COUNT(*) FROM ads WHERE ads.advertiser_id = a.id) AS n_ads,
                    (SELECT COUNT(*) FROM ads WHERE ads.advertiser_id = a.id AND ads.status = 'active') AS n_active_ads
             FROM advertisers a"""
    if not include_paused:
        sql += " WHERE a.status = 'active'"
    sql += " ORDER BY a.name COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def set_advertiser_status(conn: sqlite3.Connection, advertiser_id: int, status: str) -> None:
    assert status in ("active", "paused")
    conn.execute("UPDATE advertisers SET status = ? WHERE id = ?", (status, advertiser_id))
    conn.commit()


def update_advertiser(
    conn: sqlite3.Connection,
    advertiser_id: int,
    category: str | None = None,
    countries: list[str] | None = None,
    notes: str | None = None,
) -> None:
    if category is not None:
        conn.execute("UPDATE advertisers SET category = ? WHERE id = ?", (category, advertiser_id))
    if countries is not None:
        conn.execute(
            "UPDATE advertisers SET countries = ? WHERE id = ?",
            (",".join(countries), advertiser_id),
        )
    if notes is not None:
        conn.execute("UPDATE advertisers SET notes = ? WHERE id = ?", (notes, advertiser_id))
    conn.commit()


def add_page(
    conn: sqlite3.Connection,
    advertiser_id: int,
    page_id: str,
    page_name: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO advertiser_pages (advertiser_id, page_id, page_name)
           VALUES (?, ?, ?)
           ON CONFLICT(advertiser_id, page_id) DO UPDATE SET
             page_name = COALESCE(excluded.page_name, page_name)""",
        (advertiser_id, str(page_id), page_name),
    )
    conn.commit()


def remove_page(conn: sqlite3.Connection, advertiser_id: int, page_id: str) -> None:
    conn.execute(
        "DELETE FROM advertiser_pages WHERE advertiser_id = ? AND page_id = ?",
        (advertiser_id, str(page_id)),
    )
    conn.commit()


def pages_for(conn: sqlite3.Connection, advertiser_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM advertiser_pages WHERE advertiser_id = ? ORDER BY page_name",
        (advertiser_id,),
    ).fetchall()


# ── Categories & tags ────────────────────────────────────────────────


def add_category(
    conn: sqlite3.Connection,
    name: str,
    type_: str,
    tag_group: str | None = None,
    description: str | None = None,
) -> int:
    assert type_ in ("advertiser_category", "ad_tag")
    existing = conn.execute(
        "SELECT id FROM categories WHERE name = ? AND type = ?", (name, type_)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE categories SET tag_group = COALESCE(?, tag_group),
                                     description = COALESCE(?, description)
               WHERE id = ?""",
            (tag_group, description, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO categories (name, type, tag_group, description) VALUES (?, ?, ?, ?)",
        (name, type_, tag_group, description),
    )
    conn.commit()
    return cur.lastrowid


def list_categories(conn: sqlite3.Connection, type_: str | None = None) -> list[sqlite3.Row]:
    if type_:
        return conn.execute(
            "SELECT * FROM categories WHERE type = ? ORDER BY tag_group, name", (type_,)
        ).fetchall()
    return conn.execute("SELECT * FROM categories ORDER BY type, tag_group, name").fetchall()


def rename_category(conn: sqlite3.Connection, category_id: int, new_name: str) -> None:
    old = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, category_id))
    # advertiser categories are referenced by name — keep them in sync
    if old and old["type"] == "advertiser_category":
        conn.execute(
            "UPDATE advertisers SET category = ? WHERE category = ?", (new_name, old["name"])
        )
    conn.commit()


def delete_category(conn: sqlite3.Connection, category_id: int) -> None:
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()


def set_tag(
    conn: sqlite3.Connection,
    ad_id: str,
    category_id: int,
    source: str = "manual",
    confidence: float | None = None,
    status: str = "accepted",
) -> None:
    conn.execute(
        """INSERT INTO ad_tags (ad_id, category_id, source, confidence, status)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(ad_id, category_id) DO UPDATE SET
             source = excluded.source,
             confidence = excluded.confidence,
             status = excluded.status""",
        (ad_id, category_id, source, confidence, status),
    )
    conn.commit()


def suggest_tag(
    conn: sqlite3.Connection, ad_id: str, category_id: int, confidence: float
) -> None:
    """AI suggestion: never overwrites an existing manual/decided tag."""
    conn.execute(
        """INSERT OR IGNORE INTO ad_tags (ad_id, category_id, source, confidence, status)
           VALUES (?, ?, 'ai', ?, 'suggested')""",
        (ad_id, category_id, confidence),
    )
    conn.commit()


def decide_tag(conn: sqlite3.Connection, ad_id: str, category_id: int, accept: bool) -> None:
    conn.execute(
        "UPDATE ad_tags SET status = ? WHERE ad_id = ? AND category_id = ?",
        ("accepted" if accept else "rejected", ad_id, category_id),
    )
    conn.commit()


def remove_tag(conn: sqlite3.Connection, ad_id: str, category_id: int) -> None:
    conn.execute(
        "DELETE FROM ad_tags WHERE ad_id = ? AND category_id = ?", (ad_id, category_id)
    )
    conn.commit()


# ── Seeding from YAML ────────────────────────────────────────────────


def seed_taxonomy(conn: sqlite3.Connection, taxonomy_path: Path | str) -> int:
    """Idempotent: upserts every seed category, keeps user additions intact."""
    data = yaml.safe_load(Path(taxonomy_path).read_text(encoding="utf-8")) or {}
    n = 0
    for cat in data.get("advertiser_categories", []):
        add_category(conn, cat["name"], "advertiser_category", None, cat.get("description"))
        n += 1
    for tag in data.get("ad_tags", []):
        add_category(conn, tag["name"], "ad_tag", tag.get("group"), tag.get("description"))
        n += 1
    logger.info("Taxonomie geseed: %d categorieën/tags", n)
    return n


def seed_competitors(conn: sqlite3.Connection, seed_path: Path | str) -> int:
    """Idempotent: upserts advertisers by name. Pages come from `adscout resolve`."""
    data = yaml.safe_load(Path(seed_path).read_text(encoding="utf-8")) or {}
    n = 0
    for adv in data.get("advertisers", []):
        advertiser_id = add_advertiser(
            conn,
            name=adv["name"],
            category=adv.get("category"),
            countries=[str(c).upper() for c in adv.get("countries", ["NL"])],
            notes=adv.get("notes"),
        )
        for page in adv.get("page_ids", []) or []:
            if isinstance(page, dict):
                add_page(conn, advertiser_id, str(page["page_id"]), page.get("page_name"))
            else:
                add_page(conn, advertiser_id, str(page))
        n += 1
    logger.info("Watchlist geseed: %d concurrenten", n)
    return n
