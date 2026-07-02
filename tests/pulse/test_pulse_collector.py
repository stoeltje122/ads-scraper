"""Collector: never blocks, idempotent, audited, retention-aware."""

from __future__ import annotations

from helpers_pulse import PULSE_FIXTURE_DIR
from pulse import collector, store
from pulse.models import FeedbackItem
from pulse.sources import manual
from pulse.sources.base import SourceError


def test_fixture_collect_is_idempotent(pulse_seeded_db, pulse_settings):
    first = collector.collect(pulse_seeded_db, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    assert first.ok and first.items_new == 25
    second = collector.collect(pulse_seeded_db, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    assert second.items_new == 0 and second.items_seen == 25


def test_fixture_collect_does_not_advance_last_run(pulse_seeded_db, pulse_settings):
    collector.collect(pulse_seeded_db, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    runs = pulse_seeded_db.execute("SELECT last_run FROM sources").fetchall()
    assert all(r["last_run"] is None for r in runs)


def test_real_collect_skips_unconfigured_sources(pulse_seeded_db, pulse_settings):
    result = collector.collect(pulse_seeded_db, pulse_settings)  # no fixture dir = real mode
    assert result.ok  # skipped is not failed
    reasons = {r.source: r.skipped for r in result.per_source}
    assert reasons["Support-mail (Gmail)"] == "wacht op configuratie"
    assert reasons["Trustpilot-reviews"] == "gepauzeerd"
    assert result.items_new == 0


def test_one_failing_source_never_stops_the_run(pulse_seeded_db, pulse_settings, monkeypatch):
    for type_ in ("gmail", "meta_comments"):
        src = store.get_source(pulse_seeded_db, type_)
        store.set_source_status(pulse_seeded_db, src["id"], "active")

    class _Boom:
        def collect(self, since):
            raise SourceError("kapot")

    class _Fine:
        def collect(self, since):
            return [FeedbackItem("m-ok", "prima product")]

    monkeypatch.setattr(
        collector, "build_adapter",
        lambda type_, pulse_settings, fixture_dir=None: _Boom() if type_ == "gmail" else _Fine(),
    )
    result = collector.collect(pulse_seeded_db, pulse_settings)
    assert not result.ok
    assert result.items_new == 1  # meta still collected
    assert any("kapot" in e for e in result.errors)
    run = pulse_seeded_db.execute("SELECT * FROM runs WHERE kind='collect'").fetchone()
    assert run["ok"] == 0 and "kapot" in run["errors"]
    # gmail's last_run must NOT advance after a failure
    assert store.get_source(pulse_seeded_db, "gmail")["last_run"] is None
    assert store.get_source(pulse_seeded_db, "meta_comments")["last_run"] is not None


def test_collect_runs_retention_cleanup(pulse_seeded_db, pulse_settings):
    src = store.get_source(pulse_seeded_db, "manual")
    store.store_items(pulse_seeded_db, src["id"], [
        FeedbackItem("oud", "oude feedback", happened_at="2020-01-01"),
    ])
    result = collector.collect(pulse_seeded_db, pulse_settings)
    assert result.items_deleted == 1


def test_import_routes_channels_to_matching_sources(pulse_seeded_db):
    items = manual.parse_file(PULSE_FIXTURE_DIR / "competitor_reviews.csv")
    result = collector.import_items(pulse_seeded_db, items)
    assert result.items_new == 10
    per_source = {
        r["name"]: r["n"]
        for r in pulse_seeded_db.execute(
            "SELECT s.name, COUNT(i.id) AS n FROM sources s "
            "JOIN items i ON i.source_id = s.id GROUP BY s.id"
        )
    }
    assert per_source["Trustpilot-reviews"] == 7
    assert per_source["bol-reviews"] == 3
    run = pulse_seeded_db.execute("SELECT * FROM runs WHERE kind='import'").fetchone()
    assert run["items_new"] == 10


def test_fixture_collect_ignores_since_forever(pulse_seeded_db, pulse_settings):
    """Regression: the demo must still work years from now (fixture dates
    are fixed; the since-backfill window must not apply in fixture mode)."""
    from datetime import datetime, timezone

    far_future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    result = collector.collect(
        pulse_seeded_db, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR, now=far_future
    )
    assert result.items_new == 25


def test_import_dedupes_across_channels(pulse_seeded_db):
    """Regression: the same review pasted with and without a kanaal must
    not be stored twice."""
    from pulse.sources.manual import parse_paste

    first = parse_paste("Identieke review", competitor="8hours", channel_label="trustpilot")
    second = parse_paste("Identieke review", competitor="8hours", channel_label=None)
    collector.import_items(pulse_seeded_db, first)
    result = collector.import_items(pulse_seeded_db, second)
    assert result.items_new == 0
    assert pulse_seeded_db.execute(
        "SELECT COUNT(*) FROM items WHERE text = 'Identieke review'"
    ).fetchone()[0] == 1
