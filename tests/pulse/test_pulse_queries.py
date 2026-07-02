"""Read-side queries: filters, urgent view rules, insights."""

from __future__ import annotations

from helpers_pulse import RUN_DAY
from pulse import queries, store
from pulse.queries import ItemFilter


def test_urgent_view_health_first_and_own_brand_only(pulse_demo_db):
    items = queries.urgent_items(pulse_demo_db)
    assert len(items) == 6
    # health signals sort before the plain-urgent delivery escalation
    assert all(i["health_flag"] or i["pre_health_flag"] for i in items[:5])
    assert items[-1]["external_id"] == "msg-lev-02"
    # the Evidaplus buikpijn review is a competitor item → not in Urgent
    assert all(i["competitor_name"] is None for i in items)


def test_follow_up_removes_from_urgent(pulse_demo_db):
    items = queries.urgent_items(pulse_demo_db)
    store.mark_followed_up(pulse_demo_db, items[0]["id"])
    assert queries.count_urgent_open(pulse_demo_db) == 5
    assert len(queries.urgent_items(pulse_demo_db, include_followed_up=True)) == 6
    store.mark_followed_up(pulse_demo_db, items[0]["id"], done=False)
    assert queries.count_urgent_open(pulse_demo_db) == 6


def test_inbox_filters(pulse_demo_db):
    assert queries.count_items(pulse_demo_db, ItemFilter()) == 35
    assert queries.count_items(pulse_demo_db, ItemFilter(about="own")) == 25
    assert queries.count_items(pulse_demo_db, ItemFilter(about="competitor")) == 10
    assert queries.count_items(pulse_demo_db, ItemFilter(sentiment="positive")) > 0
    assert queries.count_items(pulse_demo_db, ItemFilter(theme="bijwerking-gezondheid")) == 6
    assert queries.count_items(pulse_demo_db, ItemFilter(q="melatonine")) >= 1
    assert queries.count_items(pulse_demo_db, ItemFilter(health_only=True)) == 6
    comp = store.get_competitor(pulse_demo_db, "8hours")
    assert queries.count_items(pulse_demo_db, ItemFilter(about=str(comp["id"]))) == 3


def test_list_items_newest_first(pulse_demo_db):
    items = queries.list_items(pulse_demo_db, ItemFilter(), limit=5)
    dates = [(i["happened_at"] or i["first_seen"]) for i in items]
    assert dates == sorted(dates, reverse=True)


def test_thread_items_grouped(pulse_demo_db):
    row = pulse_demo_db.execute(
        "SELECT thread_id FROM items WHERE external_id = 'msg-lev-01'"
    ).fetchone()
    thread = queries.thread_items(pulse_demo_db, row["thread_id"])
    assert [t["external_id"] for t in thread] == ["msg-lev-01", "msg-lev-02"]


def test_week_over_week_top_complaints(pulse_demo_db):
    wow = queries.week_over_week(pulse_demo_db, today=RUN_DAY)
    top = {t["theme"] for t in wow["complaints_now"]}
    assert "levering-verzending" in top


def test_competitor_insights(pulse_demo_db):
    insights = {i["competitor"]["name"]: i for i in queries.competitor_insights(pulse_demo_db)}
    assert insights["8hours"]["n_analyzed"] == 3
    assert len(insights["8hours"]["cons"]) == 3
    assert insights["Cloudpillo"]["pros"]
    assert insights["Ella"]["n_items"] == 0


def test_counts_summary(pulse_demo_db):
    summary = queries.counts_summary(pulse_demo_db)
    assert summary["items"] == 35
    assert summary["analyzed"] == 35
    assert summary["queue"] == 0
    assert summary["urgent_open"] == 6


def test_keyword_flag_survives_ai_disagreement(pulse_seeded_db):
    """Regression: a keyword-flagged item stays in Urgent even when the AI
    judges it not health-related — doubt means a human looks."""
    from pulse.models import AnalysisResult, FeedbackItem

    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [
        FeedbackItem("k1", "Sinds de capsules heb ik hartkloppingen"),
    ])
    item_id = pulse_seeded_db.execute("SELECT id FROM items").fetchone()[0]
    assert queries.count_urgent_open(pulse_seeded_db) == 1
    # A plausible model miss: not health, normal urgency.
    store.save_analysis(pulse_seeded_db, item_id, AnalysisResult(
        sentiment="negative", themes=["overig"], urgency="normal",
        type="complaint", health_flag=False, confidence=0.6), model="test")
    assert queries.count_urgent_open(pulse_seeded_db) == 1  # still visible
    store.mark_followed_up(pulse_seeded_db, item_id)
    assert queries.count_urgent_open(pulse_seeded_db) == 0  # human decided
