"""db: migration runner behaviour."""

from __future__ import annotations

import sqlite3

import pytest

from adscout import db

# Every .sql file in the real migrations dir, in order — tests stay valid
# when new migrations are added.
ALL_VERSIONS = sorted(
    int(p.name[:4]) for p in db.MIGRATIONS_DIR.glob("*.sql")
)


def _tables(conn) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_migrate_applies_all_migrations_once(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    applied = db.migrate(conn)

    assert applied == ALL_VERSIONS
    assert {
        "advertisers",
        "advertiser_pages",
        "ads",
        "ad_texts",
        "ad_snapshots",
        "creatives",
        "categories",
        "ad_tags",
        "runs",
        "schema_migrations",
    } <= _tables(conn)
    versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
    assert versions == ALL_VERSIONS
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    assert db.migrate(conn) == ALL_VERSIONS
    assert db.migrate(conn) == []  # nothing pending on the second pass
    count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == len(ALL_VERSIONS)
    conn.close()


def test_open_db_connects_and_migrates(tmp_path):
    conn = db.open_db(tmp_path / "auto.db")
    assert "ads" in _tables(conn)
    # Reopening the same file must not re-apply anything.
    conn2 = db.open_db(tmp_path / "auto.db")
    count = conn2.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == len(ALL_VERSIONS)
    conn.close()
    conn2.close()


@pytest.mark.parametrize("bad_name", ["001_too_short.sql", "notities.sql", "0002-dash.sql"])
def test_badly_named_migration_file_is_rejected(tmp_path, monkeypatch, bad_name):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / bad_name).write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations_dir)

    conn = db.connect(tmp_path / "x.db")
    with pytest.raises(ValueError, match=bad_name.replace(".", r"\.")):
        db.migrate(conn)
    # Nothing may have been applied.
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    conn.close()


def test_failing_migration_rolls_back_completely(tmp_path, monkeypatch):
    """A migration that fails halfway must leave NO trace: neither its early
    statements nor a schema_migrations row. (executescript would autocommit
    per statement — the runner deliberately avoids it.)"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_bad.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE broken (id INTEGER PRIMARY KEY;\n",  # syntax error
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations_dir)

    conn = db.connect(tmp_path / "x.db")
    with pytest.raises((sqlite3.OperationalError, ValueError)):
        db.migrate(conn)
    assert "half_applied" not in _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0

    # After fixing the migration, applying works — the DB was not bricked.
    (migrations_dir / "0001_bad.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE fixed (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    assert db.migrate(conn) == [1]
    assert {"half_applied", "fixed"} <= _tables(conn)
    conn.close()


def test_split_statements_handles_trigger_bodies():
    sql = (
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET x = 1;\n"
        "END;\n"
    )
    statements = db.split_statements(sql)
    assert len(statements) == 2
    assert statements[1].startswith("CREATE TRIGGER")
