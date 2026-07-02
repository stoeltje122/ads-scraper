"""Migration runner: applies once, idempotent, atomic rollback."""

from __future__ import annotations

import pytest

from pulse import db as pulse_db_mod
from pulse.db import migrate, open_db, split_statements


def test_migrations_apply_and_are_idempotent(tmp_path):
    conn = open_db(tmp_path / "a.db")
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert 1 in versions
    assert migrate(conn) == []  # second run: nothing pending
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sources", "competitors", "items", "analyses", "threads",
            "digests", "runs", "themes", "app_settings"} <= tables
    conn.close()


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text(
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;",
        encoding="utf-8",
    )
    monkeypatch.setattr(pulse_db_mod, "MIGRATIONS_DIR", migrations)
    with pytest.raises(Exception):
        open_db(tmp_path / "b.db")
    conn = pulse_db_mod.connect(tmp_path / "b.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "t1" in tables and "t2" not in tables  # broken one fully rolled back
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert applied == {1}
    conn.close()


def test_split_statements_handles_semicolons_in_strings():
    sql = "INSERT INTO x VALUES ('a;b');\nCREATE TABLE y (id INTEGER);\n"
    assert len(split_statements(sql)) == 2


def test_split_statements_rejects_unterminated():
    with pytest.raises(ValueError):
        split_statements("CREATE TABLE broken (id INTEGER")
