"""CSV import for everything that never came through an API: history
from before the integrations, channels without an API, manual stock counts.

Excel-friendly by design: the delimiter (',' or ';') is sniffed, the
encoding is utf-8-sig (Excel writes a BOM), and amounts accept the Dutch
comma via models.parse_eur_to_cents. Rows go through the exact same
store.upsert_* functions as the live collectors, so importing the same
file twice is idempotent. A bad row is reported ("regel N: …") and
skipped — one typo must never abort a 500-row import.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from compass import store
from compass.collector import RunResult, SourceResult
from compass.models import (
    AMSTERDAM,
    CHANNELS,
    AdSpendRecord,
    InventoryRecord,
    OrderRecord,
    ams_today,
    customer_hash,
    parse_eur_to_cents,
)

logger = logging.getLogger(__name__)

MAX_ERRORS_KEPT = 20

# Required columns per file kind; the optional rest is in the README.
REQUIRED_COLUMNS = {
    "orders": {"extern_id", "channel", "ordered_at", "gross_eur"},
    "spend": {"day", "campaign_id", "campaign_name", "spend_eur"},
    "inventory": {"day", "units"},
}
INVENTORY_SOURCES = ("manual", "shopify", "fixture")


@dataclass
class ImportResult:
    kind: str                        # 'orders' | 'spend' | 'inventory'
    rows: int                        # data rows read (including bad ones)
    upserted: int                    # newly inserted rows (re-import → 0)
    errors: list[str] = field(default_factory=list)  # Dutch, max 20 kept


def detect_kind(header: list[str]) -> str | None:
    """Which import kind a header row belongs to; None when unknown.

    Checked as subsets, so extra columns never break recognition —
    exports from other tools tend to carry columns we ignore.
    """
    columns = {column.strip().lstrip("﻿").lower() for column in header}
    for kind in ("orders", "spend", "inventory"):
        if REQUIRED_COLUMNS[kind] <= columns:
            return kind
    return None


# ── per-cell parsers (each raises ValueError with a Dutch message) ───


def _required_text(row: dict[str, str], column: str) -> str:
    value = row.get(column, "").strip()
    if not value:
        raise ValueError(f"{column} ontbreekt")
    return value


def _money(row: dict[str, str], column: str) -> int | None:
    text = row.get(column, "").strip()
    if not text:
        return None
    try:
        cents = parse_eur_to_cents(text)
    except ValueError:
        raise ValueError(f"{column} is geen geldig bedrag: {text!r}") from None
    return cents


def _optional_int(row: dict[str, str], column: str) -> int | None:
    text = row.get(column, "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{column} is geen geheel getal: {text!r}") from None


def _day(row: dict[str, str], column: str = "day") -> date:
    text = _required_text(row, column)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{column} is geen geldige datum: {text!r} (gebruik JJJJ-MM-DD)"
        ) from None


def _ordered_at(row: dict[str, str]) -> datetime:
    """ISO date or datetime. A bare date becomes 12:00 Amsterdam: midday
    can never flip to another calendar day, midnight can."""
    text = _required_text(row, "ordered_at")
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day, 12, 0, tzinfo=AMSTERDAM)
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"ordered_at is geen geldige datum/tijd: {text!r} "
            "(gebruik bijv. 2026-06-15 of 2026-06-15T14:32:00+02:00)"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=AMSTERDAM)
    return moment


# ── per-row record builders ──────────────────────────────────────────


def _order_record(row: dict[str, str]) -> OrderRecord:
    channel = _required_text(row, "channel").lower()
    if channel not in CHANNELS:
        raise ValueError(f"onbekend kanaal {channel!r} (gebruik shopify of bol)")
    gross = _money(row, "gross_eur")
    if gross is None:
        raise ValueError("gross_eur ontbreekt")
    status = (row.get("status", "").strip().lower()) or "paid"
    if status not in ("paid", "refunded"):
        raise ValueError(f"onbekende status {status!r} (gebruik paid of refunded)")
    payment = row.get("payment_method", "").strip().lower() or None
    # customer_ref is hashed immediately and excluded from raw: no PII
    # ever reaches the database (AVG-dataminimalisatie).
    raw = {key: value for key, value in row.items() if value and key != "customer_ref"}
    return OrderRecord(
        extern_id=_required_text(row, "extern_id"),
        channel=channel,
        ordered_at=_ordered_at(row),
        gross_cents=gross,
        units=_optional_int(row, "units") or 1,
        customer_hash=customer_hash(row.get("customer_ref") or None),
        payment_method=payment,
        status=status,
        refunded_cents=_money(row, "refunded_eur") or 0,
        raw=raw,
    )


def _spend_record(row: dict[str, str]) -> AdSpendRecord:
    spend = _money(row, "spend_eur")
    if spend is None:
        raise ValueError("spend_eur ontbreekt")
    return AdSpendRecord(
        day=_day(row),
        campaign_id=_required_text(row, "campaign_id"),
        campaign_name=row.get("campaign_name", "").strip() or None,
        spend_cents=spend,
        impressions=_optional_int(row, "impressions"),
        clicks=_optional_int(row, "clicks"),
        meta_purchases=_optional_int(row, "meta_purchases"),
        meta_purchase_value_cents=_money(row, "meta_purchase_value_eur"),
        raw={key: value for key, value in row.items() if value},
    )


def _inventory_record(row: dict[str, str]) -> InventoryRecord:
    units = _optional_int(row, "units")
    if units is None:
        raise ValueError("units ontbreekt")
    source = row.get("source", "").strip().lower() or "manual"
    if source not in INVENTORY_SOURCES:
        raise ValueError(
            f"onbekende bron {source!r} (gebruik {', '.join(INVENTORY_SOURCES)})"
        )
    return InventoryRecord(day=_day(row), units=units, source=source)


# ── the import itself ────────────────────────────────────────────────


def import_file(
    conn: sqlite3.Connection,
    path: Path,
    kind: str | None = None,
    today: date | None = None,
) -> ImportResult:
    """Import one CSV file; returns counts plus per-row errors.

    File-level problems (unreadable header, unknown/mismatched kind)
    raise ValueError — there is nothing sensible to import then. Row
    problems are soft. Afterwards the daily rollup is rebuilt over the
    touched range and the run lands in the audit trail (kind 'import').
    """
    path = Path(path)
    today = today or ams_today()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if kind is not None and kind not in REQUIRED_COLUMNS:
        raise ValueError(
            f"Onbekend importtype {kind!r} — gebruik orders, spend of inventory."
        )

    with path.open(encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        if not first_line.strip():
            raise ValueError(f"Leeg bestand: {path.name} bevat geen kolomkoppen.")
        # Sniff the delimiter from the header: Dutch Excel saves ';'.
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        handle.seek(0)

        reader = csv.reader(handle, delimiter=delimiter)
        header = [column.strip().lower() for column in next(reader)]

        detected = detect_kind(header)
        if kind is None:
            kind = detected
            if kind is None:
                raise ValueError(
                    f"Kan het type van {path.name} niet aan de kolomkoppen "
                    "herkennen. Verwacht: orders (extern_id, channel, "
                    "ordered_at, gross_eur), spend (day, campaign_id, "
                    "campaign_name, spend_eur) of inventory (day, units)."
                )
        else:
            missing = REQUIRED_COLUMNS[kind] - set(header)
            if missing:
                raise ValueError(
                    f"Kolommen voor type '{kind}' ontbreken in {path.name}: "
                    f"{', '.join(sorted(missing))}."
                )

        errors: list[str] = []
        total_errors = 0

        def add_error(message: str) -> None:
            nonlocal total_errors
            total_errors += 1
            if len(errors) < MAX_ERRORS_KEPT:
                errors.append(message)

        rows = 0
        upserted = 0
        touched_min: date | None = None
        touched_max: date | None = None

        def touch(day: date) -> None:
            nonlocal touched_min, touched_max
            touched_min = day if touched_min is None else min(touched_min, day)
            touched_max = day if touched_max is None else max(touched_max, day)

        for values in reader:
            if not any(cell.strip() for cell in values):
                continue  # blank line (Excel loves trailing ones)
            rows += 1
            line = reader.line_num
            row = {
                column: (values[i].strip() if i < len(values) else "")
                for i, column in enumerate(header)
            }
            try:
                if kind == "orders":
                    record = _order_record(row)
                    if store.upsert_order(conn, record):
                        upserted += 1
                    touch(record.order_day)
                elif kind == "spend":
                    record = _spend_record(row)
                    if store.upsert_ad_spend(conn, record):
                        upserted += 1
                    touch(record.day)
                else:
                    store.upsert_inventory(conn, _inventory_record(row))
                    upserted += 1
            except Exception as exc:  # soft: one bad row never aborts the file
                add_error(f"regel {line}: {exc}")

    conn.commit()  # bulk upserts leave the commit to us

    # Inventory snapshots do not feed daily_metrics; orders/spend do.
    # The rebuild runs through `today` so acquisition counts on days
    # after the imported range (first-order days may have shifted) heal.
    if touched_min is not None and touched_max is not None:
        try:
            store.rebuild_daily_metrics(conn, touched_min, max(touched_max, today))
        except ValueError as exc:  # "Geen kostenmodel…" — data is stored anyway
            add_error(str(exc))
            logger.error("Dagcijfers niet herbouwd na import: %s", exc)

    run = RunResult(kind="import", started_at=started_at)
    run.sources.append(
        SourceResult(
            name=f"import:{kind}",
            ok=total_errors == 0,
            fetched=rows,
            upserted=upserted,
            error=(
                None
                if total_errors == 0
                else f"{total_errors} fout(en), eerste: {errors[0]}"
            ),
            category=kind,
        )
    )
    run.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run.run_id = store.record_run(conn, run)

    logger.info(
        "Import %s (%s): %d regels, %d nieuw, %d fout(en)",
        path.name, kind, rows, upserted, total_errors,
    )
    return ImportResult(kind=kind, rows=rows, upserted=upserted, errors=errors)
