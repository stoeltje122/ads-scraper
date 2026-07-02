"""Storage: idempotent seeding & inserts, forget, retention, analyses."""

from __future__ import annotations

from pulse import store
from pulse.models import AnalysisResult, FeedbackItem, author_hash


def _item(external_id="x1", text="Prima product", author="els@voorbeeld.nl", **kw):
    return FeedbackItem(external_id=external_id, text=text, author_ref=author, **kw)


def test_seeding_is_idempotent(pulse_seeded_db):
    assert store.seed_sources(pulse_seeded_db) == 0
    n_before = pulse_seeded_db.execute("SELECT COUNT(*) FROM themes").fetchone()[0]
    from helpers_pulse import REPO_ROOT

    store.seed_themes(pulse_seeded_db, REPO_ROOT / "pulse-taxonomy.seed.yaml")
    assert pulse_seeded_db.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == n_before


def test_store_items_dedupes_and_hashes_author(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    seen, new = store.store_items(pulse_seeded_db, src["id"], [_item(), _item()])
    assert (seen, new) == (2, 1)
    row = pulse_seeded_db.execute("SELECT * FROM items").fetchone()
    assert row["author_hash"] == author_hash("els@voorbeeld.nl")
    assert row["text"] == "Prima product"
    # second run: nothing new
    _, new2 = store.store_items(pulse_seeded_db, src["id"], [_item()])
    assert new2 == 0


def test_store_items_sets_pre_health_flag(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(
        pulse_seeded_db, src["id"],
        [_item("h1", "Ik kreeg er hoofdpijn van"), _item("h2", "Snel geleverd!")],
    )
    flags = dict(
        pulse_seeded_db.execute("SELECT external_id, pre_health_flag FROM items").fetchall()
    )
    assert flags == {"h1": 1, "h2": 0}


def test_unknown_competitor_is_created_on_the_fly(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(
        pulse_seeded_db, src["id"], [_item("c1", "review", competitor_name="NieuwMerk")]
    )
    assert store.get_competitor(pulse_seeded_db, "nieuwmerk") is not None  # case-insensitive


def test_forget_by_email_and_by_hash(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [
        _item("f1", "a", author="weg@voorbeeld.nl"),
        _item("f2", "b", author="weg@voorbeeld.nl"),
        _item("f3", "c", author="blijf@voorbeeld.nl"),
    ])
    assert store.forget(pulse_seeded_db, "Weg@Voorbeeld.nl") == 2
    assert pulse_seeded_db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert store.forget(pulse_seeded_db, author_hash("blijf@voorbeeld.nl")) == 1


def test_forget_cascades_analyses(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [_item("a1", "tekst", author="p@q.nl")])
    item_id = pulse_seeded_db.execute("SELECT id FROM items").fetchone()[0]
    store.save_analysis(pulse_seeded_db, item_id, AnalysisResult(
        sentiment="neutral", themes=["overig"], urgency="normal",
        type="review", health_flag=False, confidence=0.9), model="test")
    store.forget(pulse_seeded_db, "p@q.nl")
    assert pulse_seeded_db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0


def test_save_analysis_forces_urgent_on_health(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [_item("a2", "tekst")])
    item_id = pulse_seeded_db.execute("SELECT id FROM items").fetchone()[0]
    store.save_analysis(pulse_seeded_db, item_id, AnalysisResult(
        sentiment="negative", themes=["bijwerking-gezondheid"], urgency="low",
        type="complaint", health_flag=True, confidence=0.8), model="test")
    row = pulse_seeded_db.execute("SELECT urgency, health_flag FROM analyses").fetchone()
    assert row["urgency"] == "urgent" and row["health_flag"] == 1


def test_retention_cleanup_respects_setting(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [
        _item("old", "oud item", happened_at="2023-01-15"),
        _item("new", "nieuw item", happened_at="2026-06-30"),
    ])
    deleted = store.retention_cleanup(pulse_seeded_db, now_iso="2026-07-02T00:00:00")
    assert deleted == 1  # default 24 months
    left = [r["external_id"] for r in pulse_seeded_db.execute("SELECT external_id FROM items")]
    assert left == ["new"]

    store.set_setting(pulse_seeded_db, "retention_months", "1")
    assert store.retention_cleanup(pulse_seeded_db, now_iso="2026-09-01T00:00:00") == 1
    assert pulse_seeded_db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_mark_analysis_failure_keeps_item_in_queue(pulse_seeded_db):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [_item("q1", "tekst")])
    item_id = pulse_seeded_db.execute("SELECT id FROM items").fetchone()[0]
    store.mark_analysis_failure(pulse_seeded_db, item_id, "kapotte JSON")
    row = pulse_seeded_db.execute(
        "SELECT analysis_attempts, analysis_error FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["analysis_attempts"] == 1 and "kapotte" in row["analysis_error"]
