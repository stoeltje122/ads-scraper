"""SourceAdapter interface — every feedback channel implements this.

Design rules (see PULSE.md):
- verify-first: verify() does one minimal real call and reports in plain
  Dutch what works and what does not. It never writes to the database.
- fixture mode: constructed with a fixture_dir, an adapter reads committed
  sample data shaped like the real API response, through the exact same
  parsing code. No credentials needed; the whole tool stays testable.
- never block: a source without credentials reports configured=False and
  the collector skips it; one failing source never stops the run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from pulse.config import Settings
from pulse.models import FeedbackItem, VerifyResult


class SourceError(Exception):
    """A source-level failure (network, API error). Collector logs and continues."""


class CredentialsError(SourceError):
    """Credentials missing, expired or invalid. Needs human action (see PULSE.md)."""


class SourceAdapter(ABC):
    """One feedback channel. Subclasses set `type` to their sources.type."""

    type: str = ""

    def __init__(self, settings: Settings, fixture_dir: Path | None = None) -> None:
        self.settings = settings
        self.fixture_dir = fixture_dir

    @property
    def fixture_mode(self) -> bool:
        return self.fixture_dir is not None

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when the credentials/config for the *real* route are present."""

    @abstractmethod
    def collect(self, since: datetime | None) -> list[FeedbackItem]:
        """Return all feedback items since `since` (None = full backfill).

        May raise SourceError/CredentialsError; the collector catches per
        source. Items are deduplicated downstream on (source, external_id),
        so returning something twice is harmless.
        """

    @abstractmethod
    def verify(self) -> VerifyResult:
        """One minimal call with real credentials (or a fixture self-check),
        reported in plain Dutch. Never writes to the database."""
