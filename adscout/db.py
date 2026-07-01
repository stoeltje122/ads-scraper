"""SQLite connection and migration runner.

Plain sqlite3 with numbered SQL migrations in adscout/migrations/.
Rule: never edit an applied migration — add a new numbered file.

Migrations run atomically: every statement of a migration plus its
schema_migrations bookkeeping row commit together, or roll back together.
(Deliberately NOT executescript(): that autocommits per statement, which
would leave a half-applied schema if a migration ever failed midway.)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def split_statements(sql: str) -> list[str]:
    """Split a migration script into complete SQL statements.

    Uses sqlite3.complete_statement (sqlite3_complete), which understands
    semicolons inside strings and trigger BEGIN...END bodies.
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue  # skip comments/blank lines between statements
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"Migratie eindigt met een onafgemaakt statement: {buf[:120]!r}")
    return statements


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns applied version numbers."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    pending: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise ValueError(f"Migration file with unexpected name: {path.name}")
        version = int(m.group(1))
        if version not in applied:
            pending.append((version, path))

    done: list[int] = []
    for version, path in pending:
        logger.info("Applying migration %s", path.name)
        statements = split_statements(path.read_text(encoding="utf-8"))
        try:
            conn.execute("BEGIN")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        done.append(version)
    return done


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Connect and ensure schema is current."""
    conn = connect(db_path)
    migrate(conn)
    return conn
