"""store: YAML seeding, category rename sync, tag rules."""

from __future__ import annotations

import pytest
import yaml

from conftest import REPO_ROOT

from adscout import store

COMPETITORS_SEED = REPO_ROOT / "competitors.seed.yaml"
TAXONOMY_SEED = REPO_ROOT / "taxonomy.seed.yaml"


def _counts(conn) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("advertisers", "advertiser_pages", "categories")
    }


def test_seeding_real_yaml_twice_is_idempotent(tmp_db):
    n_tax_1 = store.seed_taxonomy(tmp_db, TAXONOMY_SEED)
    n_comp_1 = store.seed_competitors(tmp_db, COMPETITORS_SEED)
    first = _counts(tmp_db)

    # Expected counts derived from the seed files themselves.
    taxonomy = yaml.safe_load(TAXONOMY_SEED.read_text(encoding="utf-8"))
    competitors = yaml.safe_load(COMPETITORS_SEED.read_text(encoding="utf-8"))
    expected_categories = len(taxonomy["advertiser_categories"]) + len(taxonomy["ad_tags"])
    expected_advertisers = len(competitors["advertisers"])
    assert first["categories"] == expected_categories == n_tax_1
    assert first["advertisers"] == expected_advertisers
    assert n_comp_1 == expected_advertisers

    n_tax_2 = store.seed_taxonomy(tmp_db, TAXONOMY_SEED)
    n_comp_2 = store.seed_competitors(tmp_db, COMPETITORS_SEED)

    assert _counts(tmp_db) == first
    assert (n_tax_2, n_comp_2) == (n_tax_1, n_comp_1)


def test_seeding_keeps_user_edits_to_notes_and_category(tmp_db):
    store.seed_competitors(tmp_db, COMPETITORS_SEED)
    adv = store.get_advertiser(tmp_db, "Cloudpillo")
    # Seed values present after first pass
    assert adv["category"] == "sleep-comfort"
    assert adv["status"] == "active"

    store.set_advertiser_status(tmp_db, adv["id"], "paused")
    store.seed_competitors(tmp_db, COMPETITORS_SEED)
    # Re-seeding never resurrects a paused advertiser.
    assert store.get_advertiser(tmp_db, "Cloudpillo")["status"] == "paused"


def test_rename_advertiser_category_syncs_advertisers(tmp_db):
    cat_id = store.add_category(tmp_db, "sleep-comfort", "advertiser_category")
    store.add_advertiser(tmp_db, "Cloudpillo", "sleep-comfort", ["NL"])
    store.add_advertiser(tmp_db, "Zelesta", "sleep-comfort", ["NL"])
    store.add_advertiser(tmp_db, "8hours", "supplement", ["NL"])

    store.rename_category(tmp_db, cat_id, "slaapcomfort")

    rows = tmp_db.execute(
        "SELECT name, category FROM advertisers ORDER BY name"
    ).fetchall()
    assert {r["name"]: r["category"] for r in rows} == {
        "8hours": "supplement",       # untouched
        "Cloudpillo": "slaapcomfort",
        "Zelesta": "slaapcomfort",
    }
    assert tmp_db.execute(
        "SELECT name FROM categories WHERE id = ?", (cat_id,)
    ).fetchone()["name"] == "slaapcomfort"


def test_rename_ad_tag_does_not_touch_advertisers(tmp_db):
    # An ad_tag that happens to share its name with an advertiser category
    tag_id = store.add_category(tmp_db, "supplement", "ad_tag", "angle")
    store.add_advertiser(tmp_db, "8hours", "supplement", ["NL"])

    store.rename_category(tmp_db, tag_id, "supplement-v2")

    assert store.get_advertiser(tmp_db, "8hours")["category"] == "supplement"


def _make_ad(conn, ad_id: str = "AD1") -> str:
    advertiser_id = store.add_advertiser(conn, f"Merk-{ad_id}", None, ["NL"])
    conn.execute(
        """INSERT INTO ads (ad_archive_id, advertiser_id, first_seen, last_seen)
           VALUES (?, ?, '2026-07-01', '2026-07-01')""",
        (ad_id, advertiser_id),
    )
    conn.commit()
    return ad_id


def test_suggest_tag_never_overwrites_manual_tag(tmp_db):
    ad_id = _make_ad(tmp_db)
    cat_id = store.add_category(tmp_db, "hook: vraag", "ad_tag", "hook")
    store.set_tag(tmp_db, ad_id, cat_id, source="manual", status="accepted")

    store.suggest_tag(tmp_db, ad_id, cat_id, confidence=0.93)

    row = tmp_db.execute(
        "SELECT * FROM ad_tags WHERE ad_id = ? AND category_id = ?", (ad_id, cat_id)
    ).fetchone()
    assert row["source"] == "manual"
    assert row["status"] == "accepted"
    assert row["confidence"] is None  # untouched by the AI suggestion


def test_suggest_tag_never_overwrites_decided_tag(tmp_db):
    ad_id = _make_ad(tmp_db)
    cat_id = store.add_category(tmp_db, "offer: bundel", "ad_tag", "offer")
    store.suggest_tag(tmp_db, ad_id, cat_id, confidence=0.6)
    store.decide_tag(tmp_db, ad_id, cat_id, accept=False)

    store.suggest_tag(tmp_db, ad_id, cat_id, confidence=0.99)  # AI tries again

    row = tmp_db.execute(
        "SELECT * FROM ad_tags WHERE ad_id = ? AND category_id = ?", (ad_id, cat_id)
    ).fetchone()
    assert row["status"] == "rejected"  # human decision stands
    assert row["confidence"] == 0.6


def test_decide_tag_accept_and_reject(tmp_db):
    ad_id = _make_ad(tmp_db)
    cat_a = store.add_category(tmp_db, "angle: inslapen", "ad_tag", "angle")
    cat_b = store.add_category(tmp_db, "angle: herstel", "ad_tag", "angle")
    store.suggest_tag(tmp_db, ad_id, cat_a, confidence=0.8)
    store.suggest_tag(tmp_db, ad_id, cat_b, confidence=0.7)

    store.decide_tag(tmp_db, ad_id, cat_a, accept=True)
    store.decide_tag(tmp_db, ad_id, cat_b, accept=False)

    status = {
        row["category_id"]: row["status"]
        for row in tmp_db.execute("SELECT * FROM ad_tags WHERE ad_id = ?", (ad_id,))
    }
    assert status == {cat_a: "accepted", cat_b: "rejected"}
    # Source stays 'ai': accepting a suggestion is not the same as manual tagging.
    sources = {r["source"] for r in tmp_db.execute("SELECT source FROM ad_tags")}
    assert sources == {"ai"}


def test_add_advertiser_on_conflict_returns_existing_id(tmp_db):
    first_id = store.add_advertiser(tmp_db, "Merk", "supplement", ["NL"])
    # Unrelated inserts move the connection-level last_insert_rowid along.
    for i in range(5):
        store.add_category(tmp_db, f"tag-{i}", "ad_tag")

    again = store.add_advertiser(tmp_db, "Merk", "supplement", ["NL"])

    assert again == first_id
