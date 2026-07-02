"""bol adapter — VERIFIED July 2026: Retailer API has no review *text*.

Verification outcome (sources in PULSE.md):
- The official bol Retailer API (v10, free with the seller account) does
  NOT expose written product reviews — no text, author or date, and the
  seller dashboard offers no export of them either.
- It DOES offer "Get product ratings" (Insights): the star distribution
  per EAN, explicitly also for competitor products. That is a possible
  future extension (automatic star tracking), noted in OCHTEND.md.
- Scraping bol product pages violates bol's terms.

Definitive route for review text: MANUAL IMPORT (dashboard paste box /
CSV). This stub feeds the demo via fixtures, explains the situation in
verify(), and reserves the spot for the ratings extension.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from pulse.models import FeedbackItem, VerifyResult, content_hash
from pulse.sources.base import SourceAdapter, SourceError

logger = logging.getLogger(__name__)

FIXTURE_FILE = "bol_reviews.json"

VERIFIED_EXPLANATION = (
    "De officiële bol Retailer API geeft geen geschreven productreviews terug "
    "(alleen de sterrenverdeling per EAN) en scrapen van productpagina's is "
    "tegen de voorwaarden. Reviewteksten gaan dus via handmatige import "
    "(dashboard → Import). Automatische sterren-tracking per EAN is een "
    "mogelijke uitbreiding — zie OCHTEND.md."
)


class BolAdapter(SourceAdapter):
    type = "bol"

    @property
    def configured(self) -> bool:
        return False  # review text has no official route — see module docstring

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
        stars = review.get("stars")
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
            raw={"stars": stars, "platform": "bol"},
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
            "Geverifieerd (juli 2026): de Retailer API ontsluit geen reviewteksten.",
            [
                VERIFIED_EXPLANATION,
                "Reviews plak je in het dashboard (Import) of importeer je als CSV.",
            ],
        )
