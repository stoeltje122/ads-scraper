"""AI pipeline: strict JSON parsing, health enforcement, queue behaviour."""

from __future__ import annotations

import json

import pytest

from helpers_pulse import PULSE_FIXTURE_DIR
from pulse import analysis, store
from pulse.analysis import (
    AnalysisError,
    Analyzer,
    AnalyzerUnavailable,
    CannedAnalyzer,
    analyze_pending,
    parse_analysis,
    pending_items,
)
from pulse.models import AnalysisResult, FeedbackItem

SLUGS = {"werking-inslapen", "bijwerking-gezondheid", "overig", "prijs-korting"}


def _raw(**overrides) -> str:
    base = {
        "sentiment": "positive", "type": "compliment", "urgency": "low",
        "health_flag": False, "themes": ["werking-inslapen"],
        "language": "nl", "confidence": 0.9,
        "competitor_pro": None, "competitor_con": None,
    }
    base.update(overrides)
    return json.dumps(base)


def test_parse_valid_analysis():
    result = parse_analysis(_raw(), SLUGS, is_competitor_item=False)
    assert result.sentiment == "positive"
    assert result.themes == ["werking-inslapen"]
    assert result.health_flag is False


def test_parse_tolerates_prose_around_json():
    raw = "Hier is de analyse:\n" + _raw() + "\nHopelijk helpt dit!"
    assert parse_analysis(raw, SLUGS, False).type == "compliment"


def test_parse_maps_dutch_synonyms():
    result = parse_analysis(
        _raw(sentiment="positief", type="klacht", urgency="normaal"), SLUGS, False
    )
    assert (result.sentiment, result.type, result.urgency) == ("positive", "complaint", "normal")


def test_parse_rejects_bad_enum():
    with pytest.raises(AnalysisError):
        parse_analysis(_raw(sentiment="fantastic"), SLUGS, False)


def test_parse_rejects_non_json():
    with pytest.raises(AnalysisError):
        parse_analysis("geen json hier", SLUGS, False)


def test_unknown_themes_fall_back_to_overig():
    result = parse_analysis(_raw(themes=["verzonnen-thema"]), SLUGS, False)
    assert result.themes == ["overig"]


def test_health_theme_forces_flag_and_urgency():
    result = parse_analysis(
        _raw(themes=["bijwerking-gezondheid"], health_flag=False, urgency="low"),
        SLUGS, False,
    )
    assert result.health_flag is True and result.urgency == "urgent"


def test_health_flag_forces_theme_and_urgency():
    result = parse_analysis(_raw(health_flag=True, urgency="low"), SLUGS, False)
    assert result.urgency == "urgent"
    assert "bijwerking-gezondheid" in result.themes


def test_competitor_fields_only_for_competitor_items():
    raw = _raw(competitor_con="te duur")
    assert parse_analysis(raw, SLUGS, is_competitor_item=False).competitor_con is None
    assert parse_analysis(raw, SLUGS, is_competitor_item=True).competitor_con == "te duur"


def test_confidence_is_clamped():
    assert parse_analysis(_raw(confidence=7), SLUGS, False).confidence == 1.0
    assert parse_analysis(_raw(confidence="kapot"), SLUGS, False).confidence == 0.5


# ── Queue behaviour ──────────────────────────────────────────────────


def _add_items(conn, items):
    src = store.get_source(conn, "manual")
    store.store_items(conn, src["id"], items)


def test_queue_prioritizes_health_and_retries_failures_last(pulse_seeded_db):
    _add_items(pulse_seeded_db, [
        FeedbackItem("a", "Snel geleverd", happened_at="2026-06-30"),
        FeedbackItem("b", "Ik kreeg er hoofdpijn van", happened_at="2026-06-01"),
    ])
    failed_id = pulse_seeded_db.execute(
        "SELECT id FROM items WHERE external_id = 'a'"
    ).fetchone()[0]
    queue = pending_items(pulse_seeded_db)
    assert queue[0]["external_id"] == "b"  # pre-health first despite older date
    store.mark_analysis_failure(pulse_seeded_db, failed_id, "x")
    queue = pending_items(pulse_seeded_db)
    assert queue[-1]["external_id"] == "a"  # failures retry after fresh items


class _FlakyAnalyzer(Analyzer):
    """Fails parsing for one specific item, succeeds otherwise."""

    model_name = "flaky-test"

    def __init__(self, bad_external_id):
        self.bad = bad_external_id

    def analyze(self, item, themes):
        if item["external_id"] == self.bad:
            raise AnalysisError("kapotte JSON")
        return AnalysisResult(
            sentiment="neutral", themes=["overig"], urgency="normal",
            type="review", health_flag=False, confidence=0.5,
        )


def test_analyze_pending_marks_failures_never_drops(pulse_seeded_db, pulse_settings):
    _add_items(pulse_seeded_db, [FeedbackItem("ok1", "prima"), FeedbackItem("bad1", "hmm")])
    outcome = analyze_pending(pulse_seeded_db, pulse_settings, analyzer=_FlakyAnalyzer("bad1"))
    assert outcome == {"analyzed": 1, "failed": 1, "remaining": 1}
    row = pulse_seeded_db.execute(
        "SELECT analysis_attempts, analysis_error FROM items WHERE external_id='bad1'"
    ).fetchone()
    assert row["analysis_attempts"] == 1 and row["analysis_error"]
    # the run is recorded in the audit trail
    run = pulse_seeded_db.execute("SELECT * FROM runs WHERE kind='analyze'").fetchone()
    assert run["items_analyzed"] == 1


class _DownAnalyzer(Analyzer):
    model_name = "down"
    calls = 0

    def analyze(self, item, themes):
        type(self).calls += 1
        raise ConnectionError("API onbereikbaar")


def test_repeated_api_failures_abort_the_run(pulse_seeded_db, pulse_settings):
    _DownAnalyzer.calls = 0
    _add_items(pulse_seeded_db, [FeedbackItem(f"i{n}", f"tekst {n}") for n in range(10)])
    analyze_pending(pulse_seeded_db, pulse_settings, analyzer=_DownAnalyzer())
    assert _DownAnalyzer.calls == analysis.MAX_CONSECUTIVE_API_FAILURES  # not all 10


def test_no_api_key_is_a_friendly_error(pulse_seeded_db, pulse_settings):
    with pytest.raises(AnalyzerUnavailable, match="ANTHROPIC_API_KEY"):
        analyze_pending(pulse_seeded_db, pulse_settings)


def test_canned_analyzer_covers_all_fixture_items(pulse_demo_db):
    """Every demo item must be analyzed (no heuristic fallbacks left over)."""
    remaining = analysis.queue_size(pulse_demo_db)
    assert remaining == 0
    low_confidence = pulse_demo_db.execute(
        "SELECT COUNT(*) FROM analyses WHERE confidence < 0.5"
    ).fetchone()[0]
    assert low_confidence == 0  # heuristic fallback uses 0.3 → none used


def test_canned_analyzer_heuristic_fallback(pulse_seeded_db, pulse_settings):
    _add_items(pulse_seeded_db, [FeedbackItem("nieuw", "Ik werd er misselijk van")])
    analyze_pending(pulse_seeded_db, pulse_settings, analyzer=CannedAnalyzer(PULSE_FIXTURE_DIR))
    row = pulse_seeded_db.execute(
        "SELECT a.health_flag, a.urgency FROM analyses a JOIN items i ON i.id = a.item_id "
        "WHERE i.external_id = 'nieuw'"
    ).fetchone()
    assert row["health_flag"] == 1 and row["urgency"] == "urgent"
