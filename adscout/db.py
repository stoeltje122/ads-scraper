"""SQLite connection and migration runner.

Plain sqlite3 with numbered SQL migrations in adscout/migrations/.
Rule: never edit an applied migration — add a new numbered file.
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


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns applied version numbers."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
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
        with conn:  # one transaction per migration
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        done.append(version)
    return done


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Connect and ensure schema is current."""
    conn = connect(db_path)
    migrate(conn)
    return conn
