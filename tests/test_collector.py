"""collector.collect: idempotency, change detection, vanish and error handling."""

from __future__ import annotations

import json
from datetime import date
from typing import Iterator

from conftest import RUN_DAY, PAGE_IDS, sample_items, seed_watchlist

from adscout.models import AdRecord
from adscout.sources.base import AdSource, AdSourceError, TokenError
from adscout.sources.fixture import FixtureAdSource

# Fixture ad ids (see tests/fixtures/ads/sample_ads_archive.json)
CLOUD_ACTIVE_1 = "1001001001001"   # Cloudpillo, running
CLOUD_STOPPED = "1001001001002"    # Cloudpillo, stop 2026-02-20
CLOUD_ACTIVE_2 = "1001001001003"   # Cloudpillo, running
ZEL_ACTIVE = "2002002002001"       # Zelesta, running
ZEL_STOPPED = "2002002002002"      # Zelesta, stop 2026-06-25
HOURS_ACTIVE = "3003003003001"     # 8hours, running

ALL_IDS = {CLOUD_ACTIVE_1, CLOUD_STOPPED, CLOUD_ACTIVE_2, ZEL_ACTIVE, ZEL_STOPPED, HOURS_ACTIVE}

DAY2 = date(2026, 7, 2)
DAY3 = date(2026, 7, 3)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _ad(conn, ad_id: str):
    return conn.execute("SELECT * FROM ads WHERE ad_archive_id = ?", (ad_id,)).fetchone()


def _result_for(result, advertiser: str):
    return next(r for r in result.per_advertiser if r.advertiser == advertiser)


class _SelectiveFailSource(AdSource):
    """Delegates to a FixtureAdSource, raising for one advertiser's pages."""

    def __init__(self, inner: AdSource, fail_page_ids: set[str], exc_type, message: str):
        self.inner = inner
        self.fail_page_ids = fail_page_ids
        self.exc_type = exc_type
        self.message = message

    def fetch_ads(self, page_ids, country, active_status="ALL") -> Iterator[AdRecord]:
        if {str(p) for p in page_ids} & self.fail_page_ids:
            raise self.exc_type(self.message)
        yield from self.inner.fetch_ads(page_ids, country, active_status)

    def search_pages(self, brand_name, country):
        return []


def test_first_collect_inserts_ads_texts_and_snapshots(collected_db):
    conn, result = collected_db

    assert _count(conn, "ads") == 6
    # Text variants: ad 1001001001001 has 2, the other five ads 1 each.
    assert _count(conn, "ad_texts") == 7
    assert _count(conn, "ad_snapshots") == 6

    day = RUN_DAY.isoformat()
    for row in conn.execute("SELECT first_seen, last_seen FROM ads"):
        assert row["first_seen"] == day
        assert row["last_seen"] == day

    # Status is inferred from ad_delivery_stop_time relative to the run date.
    statuses = {r["ad_archive_id"]: r["status"] for r in conn.execute("SELECT * FROM ads")}
    assert statuses[CLOUD_STOPPED] == "inactive"
    assert statuses[ZEL_STOPPED] == "inactive"
    for ad_id in (CLOUD_ACTIVE_1, CLOUD_ACTIVE_2, ZEL_ACTIVE, HOURS_ACTIVE):
        assert statuses[ad_id] == "active"

    assert result.new_ads == 6
    assert set(result.new_ad_ids) == ALL_IDS
    # Ads that were already stopped on first sight never count as "just stopped".
    assert result.stopped_ads == 0
    assert result.ok

    run = conn.execute("SELECT * FROM runs").fetchone()
    assert result.run_id == run["id"]
    assert run["ok"] == 1
    assert run["ads_fetched"] == 6
    assert run["new_ads"] == 6
    assert json.loads(run["errors"]) == []


def test_second_collect_same_day_is_idempotent(collected_db, fixture_source, run_collect):
    conn, _first = collected_db
    before = {t: _count(conn, t) for t in ("ads", "ad_texts", "ad_snapshots")}

    second = run_collect(conn, fixture_source, RUN_DAY)

    assert {t: _count(conn, t) for t in ("ads", "ad_texts", "ad_snapshots")} == before
    assert second.new_ads == 0
    assert second.stopped_ads == 0
    assert second.new_ad_ids == []
    assert _count(conn, "runs") == 2  # audit trail does grow, data does not


def test_ad_gaining_past_stop_date_flips_to_inactive_once(
    collected_db, make_fixture_dir, run_collect
):
    conn, _ = collected_db

    items = sample_items()
    for item in items:
        if item["id"] == CLOUD_ACTIVE_1:
            item["ad_delivery_stop_time"] = "2026-06-28"  # in the past on DAY2
    changed = FixtureAdSource(make_fixture_dir(items, "day2"))

    day2 = run_collect(conn, changed, DAY2)
    row = _ad(conn, CLOUD_ACTIVE_1)
    assert row["status"] == "inactive"
    assert row["ad_delivery_stop"] == "2026-06-28"
    assert row["last_seen"] == DAY2.isoformat()
    assert _result_for(day2, "Cloudpillo").stopped_ads == 1
    assert day2.stopped_ads == 1
    assert day2.new_ads == 0

    # Third run: the flip must be counted exactly once, not every day.
    day3 = run_collect(conn, changed, DAY3)
    assert day3.stopped_ads == 0
    assert _ad(conn, CLOUD_ACTIVE_1)["status"] == "inactive"


def test_vanished_ad_marked_inactive_with_last_seen_unchanged(
    collected_db, make_fixture_dir, run_collect
):
    conn, _ = collected_db

    items = [i for i in sample_items() if i["id"] != CLOUD_ACTIVE_2]
    shrunk = FixtureAdSource(make_fixture_dir(items, "day2"))

    day2 = run_collect(conn, shrunk, DAY2)

    vanished = _ad(conn, CLOUD_ACTIVE_2)
    assert vanished["status"] == "inactive"
    assert vanished["last_seen"] == RUN_DAY.isoformat()  # honest end proxy: last sighting
    assert vanished["ad_delivery_stop"] is None
    # No snapshot on day 2 for a vanished ad.
    seen = [r["seen_at"] for r in conn.execute(
        "SELECT seen_at FROM ad_snapshots WHERE ad_id = ?", (CLOUD_ACTIVE_2,))]
    assert seen == [RUN_DAY.isoformat()]

    assert _result_for(day2, "Cloudpillo").stopped_ads == 1
    # Still-returned ads simply moved along.
    assert _ad(conn, CLOUD_ACTIVE_1)["status"] == "active"
    assert _ad(conn, CLOUD_ACTIVE_1)["last_seen"] == DAY2.isoformat()


def test_empty_fetch_result_never_marks_ads_inactive(
    collected_db, make_fixture_dir, run_collect
):
    conn, _ = collected_db

    # 8hours suddenly returns 0 ads: anomaly guard, not "brand stopped advertising".
    items = [i for i in sample_items() if i["id"] != HOURS_ACTIVE]
    empty_for_8hours = FixtureAdSource(make_fixture_dir(items, "day2"))

    day2 = run_collect(conn, empty_for_8hours, DAY2)

    still_active = _ad(conn, HOURS_ACTIVE)
    assert still_active["status"] == "active"
    assert still_active["last_seen"] == RUN_DAY.isoformat()

    r = _result_for(day2, "8hours")
    assert r.ads_fetched == 0
    assert r.stopped_ads == 0
    assert r.error is None


def test_failing_advertiser_does_not_block_others(seeded_db, fixture_source, run_collect):
    source = _SelectiveFailSource(
        fixture_source,
        fail_page_ids={PAGE_IDS["Cloudpillo"]},
        exc_type=AdSourceError,
        message="bron kapot (test)",
    )

    result = run_collect(seeded_db, source)

    assert result.errors == ["Cloudpillo: bron kapot (test)"]
    assert not result.ok
    assert result.token_error is None
    assert _result_for(result, "Cloudpillo").error == "bron kapot (test)"
    assert _result_for(result, "Zelesta").new_ads == 2
    assert _result_for(result, "8hours").new_ads == 1
    # Only the failing advertiser is missing; the rest landed in the DB.
    ids = {r["ad_archive_id"] for r in seeded_db.execute("SELECT ad_archive_id FROM ads")}
    assert ids == {ZEL_ACTIVE, ZEL_STOPPED, HOURS_ACTIVE}

    run = seeded_db.execute("SELECT * FROM runs").fetchone()
    assert run["ok"] == 0
    assert json.loads(run["errors"]) == ["Cloudpillo: bron kapot (test)"]


def test_token_error_stops_run_but_still_records_it(seeded_db, fixture_source, run_collect):
    source = _SelectiveFailSource(
        fixture_source,
        fail_page_ids=set(PAGE_IDS.values()),  # every advertiser fails
        exc_type=TokenError,
        message="token verlopen (test)",
    )

    result = run_collect(seeded_db, source)

    assert result.token_error == "token verlopen (test)"
    # Advertisers iterate in name order: 8hours fails first, the rest is skipped.
    assert [r.advertiser for r in result.per_advertiser] == ["8hours"]
    assert result.errors == ["8hours: token verlopen (test)"]
    assert seeded_db.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 0

    # The aborted run is still in the audit trail.
    assert result.run_id is not None
    run = seeded_db.execute("SELECT * FROM runs").fetchone()
    assert run["id"] == result.run_id
    assert run["ok"] == 0
    assert json.loads(run["errors"]) == ["8hours: token verlopen (test)"]


def test_countries_merge_when_ad_seen_in_nl_and_be(tmp_db, fixture_source, run_collect):
    seed_watchlist(tmp_db, countries=["NL", "BE"])

    result = run_collect(tmp_db, fixture_source)

    # The fixture source returns the same ads for both countries → merged, sorted.
    for row in tmp_db.execute("SELECT ad_archive_id, countries FROM ads"):
        assert json.loads(row["countries"]) == ["BE", "NL"]

    zelesta = _result_for(result, "Zelesta")
    assert zelesta.ads_fetched == 2  # unique ads, not (ad x country) pairs
    assert zelesta.new_ads == 2     # each ad is new exactly once
    assert result.new_ads == 6
    # Still one snapshot per ad per day, regardless of country count.
    assert _count(tmp_db, "ad_snapshots") == 6
