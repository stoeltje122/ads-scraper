"""Normalized data records passed between adapters, collector and storage."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    """The one clock policy for all of Pulse: timezone-aware UTC.

    Every stored timestamp (first_seen, last_run, analyzed_at, run stamps)
    comes from here, so report windows and trend buckets can never shift
    across a DST switch or a laptop timezone change.
    """
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def author_hash(identifier: str | None) -> str | None:
    """Stable pseudonym for an author identifier (AVG/dataminimalisatie).

    sha256 of the normalized identifier (casefolded, trimmed, NFC). The bare
    e-mail address is never used as a key; `pulse forget <adres>` recomputes
    this hash to find the person's items.
    """
    if not identifier or not identifier.strip():
        return None
    normalized = unicodedata.normalize("NFC", identifier.strip().casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_hash(
    text: str,
    author: str | None,
    happened_at: str | None,
    competitor: str | None = None,
) -> str:
    """Dedupe key for manual imports: hash of text + author + date (+ the
    competitor, so an identical short review about two different brands is
    not collapsed into one).

    Whitespace is collapsed so re-pasting the same review with an extra
    newline does not create a duplicate.
    """
    collapsed = " ".join(text.split())
    payload = "\n".join(
        [
            collapsed,
            (author or "").strip().casefold(),
            (happened_at or "")[:10],
            (competitor or "").strip().casefold(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FeedbackItem:
    """One piece of feedback from a source, normalized. `raw` keeps the
    untouched payload; `author_ref` is the raw identifier used *only* to
    compute author_hash at store time — it is never persisted."""

    external_id: str
    text: str
    happened_at: str | None = None      # ISO datetime or date, as the source reports it
    author_display: str | None = None
    author_ref: str | None = None
    language: str | None = None
    url: str | None = None
    competitor_name: str | None = None  # resolved to competitor_id at store time
    thread_external_id: str | None = None
    thread_subject: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)


@dataclass
class VerifyResult:
    """Outcome of one adapter's verify(): a minimal real call (or fixture
    self-check) reported in plain Dutch for the founders."""

    ok: bool
    message: str
    detail: list[str] = field(default_factory=list)


@dataclass
class SourceResult:
    """Per-source outcome of one collect run."""

    source: str
    items_seen: int = 0
    items_new: int = 0
    error: str | None = None
    skipped: str | None = None          # reason: awaiting_config / paused
    warning: str | None = None


@dataclass
class RunResult:
    """Outcome of one collect run, also persisted in the runs table."""

    run_id: int | None = None
    started_at: str = ""
    finished_at: str = ""
    per_source: list[SourceResult] = field(default_factory=list)
    items_deleted: int = 0              # retention cleanup at the end of the run

    @property
    def items_seen(self) -> int:
        return sum(r.items_seen for r in self.per_source)

    @property
    def items_new(self) -> int:
        return sum(r.items_new for r in self.per_source)

    @property
    def errors(self) -> list[str]:
        return [f"{r.source}: {r.error}" for r in self.per_source if r.error]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class AnalysisResult:
    """Validated analysis of one item, ready to store."""

    sentiment: str                       # positive | neutral | negative
    themes: list[str]                    # active taxonomy slugs
    urgency: str                         # urgent | normal | low
    type: str                            # complaint | question | compliment | suggestion | review
    health_flag: bool
    confidence: float
    language: str | None = None
    competitor_pro: str | None = None
    competitor_con: str | None = None
