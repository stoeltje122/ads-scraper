"""queries: winners, change windows, AdFilter combinations, volume series."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import RUN_DAY, seed_watchlist

from adscout import collector, queries
from adscout.queries import AdFilter

CLOUD_ACTIVE_1 = "1001001001001"   # start 2026-03-02, running
CLOUD_STOPPED = "1001001001002"    # start 2026-01-12, stop 2026-02-20
CLOUD_ACTIVE_2 = "1001001001003"   # start 2025-11-06, running (longest)
ZEL_ACTIVE = "2002002002001"       # start 2026-04-16, running
ZEL_STOPPED = "2002002002002"      # start 2026-06-01, stop 2026-06-25
HOURS_ACTIVE = "3003003003001"     # start 2026-02-02, running


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _ids(rows) -> list[str]:
    return [r["ad_archive_id"] for r in rows]


def _advertiser_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM advertisers WHERE name = ?", (name,)).fetchone()["id"]


def test_winners_active_only_ordered_by_runtime_desc(collected_db):
    conn, _ = collected_db
    rows = queries.winners(conn)

    day = RUN_DAY.isoformat()
    expected = [
        (CLOUD_ACTIVE_2, _days("2025-11-06", day)),
        (HOURS_ACTIVE, _days("2026-02-02", day)),
        (CLOUD_ACTIVE_1, _days("2026-03-02", day)),
        (ZEL_ACTIVE, _days("2026-04-16", day)),
    ]
    assert [(r["ad_archive_id"], r["runtime_days"]) for r in rows] == expected
    assert all(r["status"] == "active" for r in rows)


def test_winners_filtered_by_advertiser_category(collected_db):
    conn, _ = collected_db
    rows = queries.winners(conn, category="supplement")
    assert _ids(rows) == [HOURS_ACTIVE]
    assert rows[0]["advertiser_name"] == "8hours"


def test_new_since_window(collected_db):
    conn, _ = collected_db
    # Everything was first seen on RUN_DAY; the boundary itself is inclusive.
    assert len(queries.new_since(conn, RUN_DAY)) == 6
    assert len(queries.new_since(conn, RUN_DAY - timedelta(days=7))) == 6
    assert queries.new_since(conn, RUN_DAY + timedelta(days=1)) == []


def test_stopped_since_uses_stop_day_and_orders_recent_first(collected_db):
    conn, _ = collected_db
    # Only the 2026-06-25 stop falls in the last week before RUN_DAY.
    assert _ids(queries.stopped_since(conn, RUN_DAY - timedelta(days=7))) == [ZEL_STOPPED]
    # A wider window catches both stopped ads, most recent stop first.
    both = queries.stopped_since(conn, date(2026, 2, 20))
    assert _ids(both) == [ZEL_STOPPED, CLOUD_STOPPED]
    # Boundary: a stop exactly on `since` counts.
    assert CLOUD_STOPPED in _ids(queries.stopped_since(conn, date(2026, 2, 20)))
    assert _ids(queries.stopped_since(conn, date(2026, 2, 21))) == [ZEL_STOPPED]


def test_filter_status(collected_db):
    conn, _ = collected_db
    assert len(queries.query_ads(conn, AdFilter(status="active"))) == 4
    assert set(_ids(queries.query_ads(conn, AdFilter(status="inactive")))) == {
        CLOUD_STOPPED,
        ZEL_STOPPED,
    }


def test_filter_advertiser(collected_db):
    conn, _ = collected_db
    cloudpillo = _advertiser_id(conn, "Cloudpillo")
    rows = queries.query_ads(conn, AdFilter(advertiser_id=cloudpillo))
    assert set(_ids(rows)) == {CLOUD_ACTIVE_1, CLOUD_STOPPED, CLOUD_ACTIVE_2}
    assert all(r["advertiser_name"] == "Cloudpillo" for r in rows)


def test_filter_format(collected_db):
    conn, _ = collected_db
    conn.execute("UPDATE ads SET format = 'video' WHERE ad_archive_id = ?", (ZEL_ACTIVE,))
    conn.commit()
    assert _ids(queries.query_ads(conn, AdFilter(format="video"))) == [ZEL_ACTIVE]
    assert len(queries.query_ads(conn, AdFilter(format="unknown"))) == 5


def test_filter_search_in_body_and_title(collected_db):
    conn, _ = collected_db
    # "dekbed" only appears in Zelesta's running ad (body + title).
    assert _ids(queries.query_ads(conn, AdFilter(search="dekbed"))) == [ZEL_ACTIVE]
    # "Probeer 100 nachten" is a link *title* variant of Cloudpillo's ad.
    assert _ids(queries.query_ads(conn, AdFilter(search="Probeer 100 nachten"))) == [
        CLOUD_ACTIVE_1
    ]
    assert queries.query_ads(conn, AdFilter(search="bestaat-niet-xyz")) == []


def test_filter_min_runtime(collected_db):
    conn, _ = collected_db
    rows = queries.query_ads(conn, AdFilter(min_runtime=100))
    assert set(_ids(rows)) == {CLOUD_ACTIVE_2, HOURS_ACTIVE, CLOUD_ACTIVE_1}


def test_filter_combination(collected_db):
    conn, _ = collected_db
    cloudpillo = _advertiser_id(conn, "Cloudpillo")
    rows = queries.query_ads(
        conn,
        AdFilter(status="active", advertiser_id=cloudpillo, min_runtime=100, sort="longest"),
    )
    assert _ids(rows) == [CLOUD_ACTIVE_2, CLOUD_ACTIVE_1]

    # Same filter, tightened until nothing matches.
    none = queries.query_ads(
        conn, AdFilter(status="inactive", advertiser_id=cloudpillo, min_runtime=100)
    )
    assert none == []


def test_count_ads_matches_query(collected_db):
    conn, _ = collected_db
    f = AdFilter(status="active", limit=2)  # limit must not affect the count
    assert queries.count_ads(conn, f) == 4
    assert len(queries.query_ads(conn, f)) == 2


def test_volume_series_shape(tmp_db, fixture_source):
    # volume_series windows on date.today(), so collect near today.
    seed_watchlist(tmp_db)
    day1 = date.today() - timedelta(days=1)
    day2 = date.today()
    collector.collect(tmp_db, fixture_source, run_date=day1)
    collector.collect(tmp_db, fixture_source, run_date=day2)

    cloudpillo = _advertiser_id(tmp_db, "Cloudpillo")
    rows = queries.volume_series(tmp_db, cloudpillo, days=90)

    assert [r["seen_at"] for r in rows] == [day1.isoformat(), day2.isoformat()]
    # Cloudpillo: 2 running ads + 1 long-stopped ad on both days.
    assert [r["active"] for r in rows] == [2, 2]
    assert set(rows[0].keys()) == {"seen_at", "active"}
