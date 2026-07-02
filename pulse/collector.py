"""The collect run: every active source, one never blocks another.

Guarantees:
- Idempotent: running twice adds nothing (dedupe on source + external_id).
- One failing source never stops the run; its error lands in the runs
  table and in `pulse status`.
- A source without credentials is skipped with reason 'wacht op
  configuratie' — the rest continues.
- Retention cleanup (default 24 months, Beheer-instelling) runs at the end
  of every collect.

Fixture mode (--fixtures) collects committed sample data for *every*
non-manual source regardless of status, so the dashboard demo works
without any credentials. Fixture runs do not advance sources.last_run:
the first real run must still do its full backfill.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pulse import store
from pulse.config import Settings
from pulse.models import FeedbackItem, RunResult, SourceResult, utc_now, utc_now_iso
from pulse.sources import build_adapter
from pulse.sources.base import CredentialsError, SourceError
from pulse.sources.manual import channel_to_source_type

logger = logging.getLogger(__name__)


def collect(
    conn: sqlite3.Connection,
    settings: Settings,
    fixture_dir: Path | None = None,
    now: datetime | None = None,
) -> RunResult:
    now = now or utc_now()
    result = RunResult(started_at=utc_now_iso())

    for source in conn.execute("SELECT * FROM sources ORDER BY id").fetchall():
        if source["type"] == "manual":
            continue  # manual items arrive via `pulse import` / dashboard
        sr = SourceResult(source=source["name"])
        result.per_source.append(sr)

        if fixture_dir is None:
            if source["status"] == "paused":
                sr.skipped = "gepauzeerd"
                continue
            if source["status"] == "awaiting_config":
                sr.skipped = "wacht op configuratie"
                continue

        try:
            adapter = build_adapter(source["type"], settings, fixture_dir=fixture_dir)
            # Fixture mode ignores `since`: the committed sample data has
            # fixed dates and must keep working (demo, tests) forever.
            since = None if fixture_dir is not None else _since_for(source, settings, now)
            items = adapter.collect(since)
            sr.items_seen, sr.items_new = store.store_items(conn, source["id"], items)
            if not items and fixture_dir is None and not source["last_run"]:
                # Only the very first real run warns: an empty backfill
                # usually means misconfiguration. Later quiet days are normal.
                sr.warning = "0 items opgehaald — klopt de configuratie?"
            if fixture_dir is None:
                store.touch_source_run(conn, source["id"], now.isoformat(timespec="seconds"))
        except CredentialsError as exc:
            sr.error = str(exc)
            logger.error("Bron %s: credentials-probleem: %s", source["name"], exc)
        except SourceError as exc:
            sr.error = str(exc)
            logger.error("Bron %s faalde: %s", source["name"], exc)
        except Exception as exc:  # a bug in one adapter never kills the run
            sr.error = f"Onverwachte fout: {exc}"
            logger.exception("Bron %s: onverwachte fout", source["name"])

    result.items_deleted = store.retention_cleanup(conn, now.isoformat(timespec="seconds"))
    result.finished_at = utc_now_iso()
    result.run_id = store.record_run(
        conn,
        kind="collect",
        started_at=result.started_at,
        finished_at=result.finished_at,
        ok=result.ok,
        items_seen=result.items_seen,
        items_new=result.items_new,
        items_deleted=result.items_deleted,
        errors=result.errors,
        detail=[vars(sr) for sr in result.per_source],
    )
    logger.info(
        "Collect-run #%s: %d items gezien, %d nieuw, %d fouten",
        result.run_id, result.items_seen, result.items_new, len(result.errors),
    )
    return result


def _since_for(source, settings: Settings, now: datetime) -> datetime:
    """Continue from the last successful run, or backfill on the first."""
    if source["last_run"]:
        try:
            return datetime.fromisoformat(source["last_run"])
        except ValueError:
            pass
    return now - timedelta(days=settings.mail_backfill_days)


def import_items(conn: sqlite3.Connection, items: list[FeedbackItem]) -> RunResult:
    """Store manually imported items, routed to the right channel.

    A row with kanaal 'trustpilot' attaches to the Trustpilot source so the
    dashboard filters work; everything else lands under 'Handmatige
    import'. Dedupe is cross-channel: the same review pasted once with and
    once without a kanaal still counts as one (content hashes are global
    enough that a match across sources is the same text, not a collision).
    Recorded in the runs table like any other run.
    """
    result = RunResult(started_at=utc_now_iso())

    known: set[str] = set()
    ids = [item.external_id for item in items]
    for offset in range(0, len(ids), 500):  # sqlite parameter limit safety
        chunk = ids[offset:offset + 500]
        known.update(
            row[0]
            for row in conn.execute(
                f"SELECT external_id FROM items WHERE external_id IN "
                f"({','.join('?' for _ in chunk)})",
                chunk,
            )
        )

    by_type: dict[str, list[FeedbackItem]] = {}
    for item in items:
        channel = (item.raw or {}).get("kanaal")
        by_type.setdefault(channel_to_source_type(channel), []).append(item)

    for source_type, group in sorted(by_type.items()):
        source = store.get_source(conn, source_type)
        if source is None:  # unknown type can only mean a corrupted sources table
            source = store.get_source(conn, "manual")
        sr = SourceResult(source=source["name"])
        fresh = [i for i in group if i.external_id not in known]
        _, sr.items_new = store.store_items(conn, source["id"], fresh)
        sr.items_seen = len(group)  # cross-channel duplicates count as seen
        result.per_source.append(sr)

    result.finished_at = utc_now_iso()
    result.run_id = store.record_run(
        conn,
        kind="import",
        started_at=result.started_at,
        finished_at=result.finished_at,
        ok=True,
        items_seen=result.items_seen,
        items_new=result.items_new,
        detail=[vars(sr) for sr in result.per_source],
    )
    logger.info(
        "Import-run #%s: %d items, %d nieuw (%d dubbel overgeslagen)",
        result.run_id, result.items_seen, result.items_new,
        result.items_seen - result.items_new,
    )
    return result
