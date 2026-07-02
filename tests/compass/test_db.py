"""db.py migrations + models.py helpers (day policy, hashing, formatting)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from compass.db import open_db
from compass.models import ams_day, customer_hash, fmt_eur


def test_migrations_create_all_tables(tmp_db):
    tables = {
        row["name"]
        for row in tmp_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "orders", "ad_spend_daily", "cost_model", "inventory_snapshots",
        "signals", "daily_metrics", "runs", "app_settings", "schema_migrations",
    } <= tables


def test_open_db_twice_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    conn = open_db(path)
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    conn = open_db(path)
    versions2 = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    assert versions == versions2 != set()


def test_ams_day_shifts_utc_evening_to_next_local_day():
    # 23:30 UTC on a summer evening is 01:30 the next day in Amsterdam.
    assert ams_day(datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)) == date(2026, 7, 1)
    # Winter (CET, +01:00): 23:30 UTC is also the next local day.
    assert ams_day(datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)) == date(2026, 1, 16)
    assert ams_day(datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc)) == date(2026, 1, 15)


def test_customer_hash_normalizes_and_never_stores_the_input():
    a = customer_hash("Anna@Example.com ")
    b = customer_hash("anna@example.com")
    assert a == b
    assert a is not None and len(a) == 64 and "anna" not in a
    assert customer_hash("") is None
    assert customer_hash(None) is None


def test_fmt_eur_dutch_formatting():
    assert fmt_eur(123456) == "€ 1.234,56"
    assert fmt_eur(2995) == "€ 29,95"
    assert fmt_eur(-440) == "-€ 4,40"
    assert fmt_eur(123456, decimals=0) == "€ 1.234"
    assert fmt_eur(None) == "—"
