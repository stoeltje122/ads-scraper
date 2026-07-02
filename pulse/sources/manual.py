"""Manual import — a first-class channel, not an afterthought.

Three entry points, all landing in the same pipeline (dedupe + AI
analysis) as automatically collected items:
(a) CSV upload (dashboard → Import, or `pulse import bestand.csv`)
(b) the paste box in the dashboard (source + competitor choice)
(c) `pulse import <bestand>` (.csv or .txt)

Dedupe: the external_id is a hash of text + author + date, so importing
the same file twice — or re-pasting the same review — never duplicates.

CSV headers are flexible (Dutch or English, any order); only a text
column is required. The optional 'kanaal' column routes a row to the
matching source (trustpilot/bol/...), so manually imported Trustpilot
reviews show up under the Trustpilot filter.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from pathlib import Path

from pulse.models import FeedbackItem, VerifyResult, content_hash
from pulse.sources.base import SourceAdapter, SourceError

logger = logging.getLogger(__name__)

# Recognized header names per field (casefolded). First match wins.
_HEADER_ALIASES: dict[str, list[str]] = {
    "text": ["tekst", "text", "review", "bericht", "comment", "inhoud", "message", "body"],
    "date": ["datum", "date", "geplaatst", "posted", "created"],
    "author": ["auteur", "author", "naam", "name", "klant", "gebruiker", "user"],
    "competitor": ["concurrent", "competitor", "merk", "brand"],
    "url": ["url", "link"],
    "language": ["taal", "language", "lang"],
    "channel": ["kanaal", "bron", "channel", "source", "platform"],
    "stars": ["sterren", "stars", "rating", "score"],
    "title": ["titel", "title", "kop"],
}

# Values in a 'kanaal' column → sources.type. Unknown labels fall back to
# 'manual' (the label itself is kept in raw_json).
CHANNEL_ALIASES: dict[str, str] = {
    "trustpilot": "trustpilot",
    "bol": "bol",
    "bol.com": "bol",
    "gmail": "gmail",
    "mail": "gmail",
    "e-mail": "gmail",
    "email": "gmail",
    "facebook": "meta_comments",
    "instagram": "meta_comments",
    "meta": "meta_comments",
    "social": "meta_comments",
}

_DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%Y-%m-%dT%H:%M:%S"]

# Paste box: blocks separated by a line containing only dashes.
_PASTE_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)


class ManualImportAdapter(SourceAdapter):
    """Adapter shell so manual import shows up in `pulse sources` and
    verify. The actual parsing lives in module functions (also used by the
    dashboard, which has no adapter instance at hand)."""

    type = "manual"

    @property
    def configured(self) -> bool:
        return True  # nothing to configure — it always works

    def collect(self, since: datetime | None) -> list[FeedbackItem]:
        return []  # manual items arrive via `pulse import` / dashboard, not collect

    def verify(self) -> VerifyResult:
        return VerifyResult(
            True,
            "Handmatige import staat altijd klaar: dashboard → Import, of "
            "`pulse import <bestand.csv/.txt>`.",
        )


def parse_file(path: Path | str) -> list[FeedbackItem]:
    """`pulse import <bestand>`: .csv → parse_csv, anything else → paste format."""
    path = Path(path)
    if not path.exists():
        raise SourceError(f"Bestand niet gevonden: {path}")
    raw = path.read_text(encoding="utf-8-sig")  # utf-8-sig: Excel exports carry a BOM
    if path.suffix.casefold() == ".csv":
        return parse_csv(raw)
    return parse_paste(raw)


def parse_csv(content: str) -> list[FeedbackItem]:
    """Parse CSV content with flexible Dutch/English headers.

    Raises SourceError with a plain-language message when no usable text
    column exists; rows without text are skipped with a warning count.
    """
    try:
        dialect = csv.Sniffer().sniff(content[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # default comma
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise SourceError("Leeg CSV-bestand — niets te importeren.")

    mapping = _map_headers(reader.fieldnames)
    if "text" not in mapping:
        raise SourceError(
            "Geen tekstkolom gevonden. Gebruik een kolomkop als 'tekst' of "
            f"'review'. Gevonden koppen: {', '.join(reader.fieldnames)}"
        )

    items: list[FeedbackItem] = []
    skipped = 0
    for row in reader:
        item = _row_to_item({k: (row.get(col) or "").strip() for k, col in mapping.items()})
        if item is None:
            skipped += 1
            continue
        items.append(item)
    if skipped:
        logger.warning("CSV-import: %d rijen zonder tekst overgeslagen", skipped)
    if not items:
        raise SourceError("Geen rijen met tekst gevonden in het CSV-bestand.")
    return items


def parse_paste(
    content: str,
    competitor: str | None = None,
    channel_label: str | None = None,
    date: str | None = None,
) -> list[FeedbackItem]:
    """Parse pasted text: one item, or several separated by a `---` line.

    competitor/channel/date come from the dashboard form fields and apply
    to every pasted block.
    """
    blocks = [b.strip() for b in _PASTE_SEPARATOR.split(content) if b.strip()]
    if not blocks:
        raise SourceError("Geen tekst geplakt — niets te importeren.")
    items = []
    for block in blocks:
        items.append(
            _row_to_item(
                {
                    "text": block,
                    "competitor": (competitor or "").strip(),
                    "channel": (channel_label or "").strip(),
                    "date": (date or "").strip(),
                }
            )
        )
    return [i for i in items if i]


def channel_to_source_type(label: str | None) -> str:
    """'Trustpilot' → 'trustpilot', unknown/empty → 'manual'."""
    if not label:
        return "manual"
    return CHANNEL_ALIASES.get(label.strip().casefold(), "manual")


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map our field names to the actual CSV column names."""
    normalized = {(name or "").strip().casefold(): name for name in fieldnames}
    mapping: dict[str, str] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    return mapping


def _row_to_item(row: dict[str, str]) -> FeedbackItem | None:
    text = row.get("text", "").strip()
    if not text:
        return None
    title = row.get("title", "").strip()
    stars = row.get("stars", "").strip()
    if title:
        text = f"{title}\n\n{text}"
    if stars:
        text = f"[{stars}/5 sterren] {text}" if stars.isdigit() else f"[{stars}] {text}"

    date = _normalize_date(row.get("date", ""))
    author = row.get("author", "").strip() or None
    channel = row.get("channel", "").strip()
    return FeedbackItem(
        external_id=content_hash(text, author, date),
        text=text,
        happened_at=date,
        author_display=author,
        author_ref=author,
        language=row.get("language", "").strip() or None,
        url=row.get("url", "").strip() or None,
        competitor_name=row.get("competitor", "").strip() or None,
        raw={"import": "manual", "kanaal": channel or None},
    )


def _normalize_date(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:  # last resort: full ISO with timezone
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        logger.warning("Datum niet herkend, genegeerd: %r", value)
        return None
