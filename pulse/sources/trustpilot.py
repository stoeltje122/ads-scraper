"""Trustpilot adapter — VERIFIED July 2026: no free automated route.

Verification outcome (sources in PULSE.md):
- Trustpilot's official API exists but requires a paid plan with the
  API/Connect module (thousands of euros/year); a free business profile
  gets no API access.
- Competitor reviews are only available via the paid "Data Solutions"
  enterprise product.
- Scraping public review pages is explicitly forbidden in Trustpilot's
  terms of use; third-party "Trustpilot APIs" are scrapers and off-limits.

Definitive route for now: MANUAL IMPORT (dashboard paste box / CSV, both
first-class citizens). This stub stays so that (a) fixture mode feeds the
demo dashboard, (b) verify() explains the situation in plain Dutch, and
(c) a future paid API integration has an obvious home.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from pulse.models import FeedbackItem, VerifyResult, content_hash
from pulse.sources.base import SourceAdapter, SourceError

logger = logging.getLogger(__name__)

FIXTURE_FILE = "trustpilot_reviews.json"

VERIFIED_EXPLANATION = (
    "Trustpilot biedt geen gratis API: automatisch ophalen vereist een betaald "
    "abonnement met API-module en reviews van concurrenten alleen het betaalde "
    "'Data Solutions'-product. Scrapen verbieden de voorwaarden uitdrukkelijk. "
    "De route voor nu is handmatige import (dashboard → Import)."
)


class TrustpilotAdapter(SourceAdapter):
    type = "trustpilot"

    @property
    def configured(self) -> bool:
        return False  # no legitimate free route — see module docstring

    def collect(self, since: datetime | None) -> list[FeedbackItem]:
        if self.fixture_mode:
            return [self.parse_review(r) for r in self._fixture_reviews()]
        raise SourceError(VERIFIED_EXPLANATION)

    def _fixture_reviews(self) -> list[dict]:
        payload = json.loads((self.fixture_dir / FIXTURE_FILE).read_text(encoding="utf-8"))
        return payload.get("reviews", [])

    def parse_review(self, review: dict) -> FeedbackItem:
        """Fixture/import shape: our own normalized review record (there is
        no official API response to mirror)."""
        text = review.get("text", "").strip()
        title = (review.get("title") or "").strip()
        stars = review.get("stars")
        if title:
            text = f"{title}\n\n{text}".strip()
        if stars:
            text = f"[{stars}/5 sterren] {text}"
        return FeedbackItem(
            external_id=review.get("id")
            or content_hash(text, review.get("author"), review.get("date")),
            text=text,
            happened_at=review.get("date"),
            author_display=review.get("author"),
            author_ref=review.get("author"),
            language=review.get("language"),
            url=review.get("url"),
            competitor_name=review.get("competitor"),
            raw={"stars": stars, "platform": "trustpilot"},
        )

    def verify(self) -> VerifyResult:
        if self.fixture_mode:
            try:
                n = len(self._fixture_reviews())
            except (OSError, ValueError) as exc:
                return VerifyResult(False, f"Fixture-bestand onleesbaar: {exc}")
            return VerifyResult(True, f"Fixture-modus: {n} voorbeeldreviews leesbaar.")
        return VerifyResult(
            False,
            "Geverifieerd (juli 2026): geen automatische route zonder betaald plan.",
            [
                VERIFIED_EXPLANATION,
                "Reviews plak je in het dashboard (Import) of importeer je als CSV.",
                "Komt er ooit een betaald plan met API-module, dan kan de koppeling "
                "alsnog gebouwd worden — de plek in de code staat er klaar voor.",
            ],
        )
