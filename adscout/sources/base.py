"""AdSource interface — every ad data provider implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from adscout.models import AdRecord, PageCandidate


class AdSourceError(Exception):
    """A source-level failure (network, API error). Collector logs and continues."""


class TokenError(AdSourceError):
    """Access token expired or invalid (Meta error 190). Needs human action."""


class PartialFetchError(AdSourceError):
    """The fetch yielded results but part of the (historic) set failed.

    Raised at the END of iteration, after everything fetchable was yielded.
    The collector treats this as a warning for inactive-history passes: the
    active picture stays trustworthy, only old history is incomplete.
    """


class AdSource(ABC):
    @abstractmethod
    def fetch_ads(
        self,
        page_ids: list[str],
        country: str,
        active_status: str = "ALL",
    ) -> Iterator[AdRecord]:
        """Yield all ads for the given pages in one country.

        active_status: ALL | ACTIVE | INACTIVE. Use ALL in the daily run so
        delivery-stop dates of finished ads are captured too.
        """

    @abstractmethod
    def search_pages(self, brand_name: str, country: str) -> list[PageCandidate]:
        """Find candidate pages advertising under a brand name.

        Used by `adscout resolve`. Candidates are *always* confirmed by a
        human — never auto-picked.
        """
