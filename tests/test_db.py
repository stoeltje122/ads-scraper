"""db: migration runner behaviour."""

from __future__ import annotations

import pytest

from adscout import db


def _tables(conn) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_migrate_applies_initial_migration_once(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    applied = db.migrate(conn)

    assert applied == [1]
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
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    assert db.migrate(conn) == [1]
    assert db.migrate(conn) == []  # nothing pending on the second pass
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    conn.close()


def test_open_db_connects_and_migrates(tmp_path):
    conn = db.open_db(tmp_path / "auto.db")
    assert "ads" in _tables(conn)
    # Reopening the same file must not re-apply anything.
    conn2 = db.open_db(tmp_path / "auto.db")
    assert conn2.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
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
