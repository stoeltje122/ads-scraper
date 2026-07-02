"""AI analysis of feedback items via the Claude API.

Strictly optional at collect time: without ANTHROPIC_API_KEY collecting
continues and items wait in the analysis queue (`pulse status` says so in
a friendly way). With a key, `pulse analyze` works the queue down — only
new items, batched by --limit, cost-conscious.

Parse failures are marked for re-analysis (analysis_attempts +
analysis_error on the item) and retried on the next run — never silently
skipped. Health enforcement is layered: the prompt instructs, this module
enforces (bijwerking-theme → health_flag), and store.save_analysis forces
urgency='urgent' for any health flag.

The `anthropic` package is imported lazily (install via
requirements-ai.txt); tests and the demo use offline analyzers.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from pulse import store
from pulse.config import Settings
from pulse.models import AnalysisResult, utc_now_iso
from pulse.prompts.feedback_analysis import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

HEALTH_THEME = "bijwerking-gezondheid"
FALLBACK_THEME = "overig"

_SENTIMENTS = {"positive", "neutral", "negative"}
_URGENCIES = {"urgent", "normal", "low"}
_TYPES = {"complaint", "question", "compliment", "suggestion", "review"}

# The model occasionally answers in Dutch despite instructions; map instead
# of wasting a re-analysis round on it.
_SYNONYMS = {
    "positief": "positive", "neutraal": "neutral", "negatief": "negative",
    "klacht": "complaint", "vraag": "question", "compliment": "compliment",
    "suggestie": "suggestion", "review": "review",
    "normaal": "normal", "laag": "low",
}

# Abort an analyze run after this many *consecutive* API failures (outage,
# invalid key): no point burning through the whole queue.
MAX_CONSECUTIVE_API_FAILURES = 3


class AnalysisError(Exception):
    """One item could not be analyzed (bad JSON, invalid fields). The item
    is marked for re-analysis; the run continues."""


class AnalyzerUnavailable(RuntimeError):
    """No key/package, or the API rejects us — the whole run must stop."""


# ── Queue ────────────────────────────────────────────────────────────


def pending_items(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """The analysis queue: newest first, possible health signals up front,
    earlier failures at the back (they retry after fresh items)."""
    return conn.execute(
        """SELECT i.*, s.name AS source_name, s.type AS source_type,
                  c.name AS competitor_name
           FROM items i
           JOIN sources s ON s.id = i.source_id
           LEFT JOIN competitors c ON c.id = i.competitor_id
           LEFT JOIN analyses a ON a.item_id = i.id
           WHERE a.item_id IS NULL
           ORDER BY i.analysis_attempts ASC, i.pre_health_flag DESC,
                    COALESCE(i.happened_at, i.first_seen) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def queue_size(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN analyses a ON a.item_id = i.id "
        "WHERE a.item_id IS NULL"
    ).fetchone()[0]


def analyze_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    limit: int = 200,
    analyzer: "Analyzer | None" = None,
) -> dict:
    """Work the queue down. Returns {'analyzed': n, 'failed': n, 'remaining': n}."""
    analyzer = analyzer or ClaudeAnalyzer(settings)
    themes = store.list_themes(conn)
    if not themes:
        raise AnalyzerUnavailable("Geen thema's in de database — draai eerst `pulse init`.")
    items = pending_items(conn, limit=limit)

    started = utc_now_iso()
    analyzed = failed = 0
    consecutive_api_failures = 0
    errors: list[str] = []
    for item in items:
        try:
            result = analyzer.analyze(item, themes)
        except AnalysisError as exc:
            failed += 1
            store.mark_analysis_failure(conn, item["id"], str(exc))
            logger.warning("Item %d gemarkeerd voor heranalyse: %s", item["id"], exc)
            continue
        except AnalyzerUnavailable:
            raise
        except Exception as exc:  # network/API errors: mark, maybe abort
            failed += 1
            consecutive_api_failures += 1
            store.mark_analysis_failure(conn, item["id"], f"API-fout: {exc}")
            errors.append(f"item {item['id']}: {exc}")
            logger.error("API-fout bij item %d: %s", item["id"], exc)
            if consecutive_api_failures >= MAX_CONSECUTIVE_API_FAILURES:
                errors.append("Run afgebroken na herhaalde API-fouten.")
                logger.error("Analyse afgebroken na %d opeenvolgende API-fouten.",
                             consecutive_api_failures)
                break
            continue
        consecutive_api_failures = 0
        store.save_analysis(conn, item["id"], result, model=analyzer.model_name)
        analyzed += 1

    remaining = queue_size(conn)
    store.record_run(
        conn,
        kind="analyze",
        started_at=started,
        finished_at=utc_now_iso(),
        ok=not errors,
        items_analyzed=analyzed,
        errors=errors,
        detail=[{"analyzed": analyzed, "failed": failed, "remaining": remaining}],
    )
    logger.info("Analyse-run: %d geanalyseerd, %d mislukt, %d in wachtrij",
                analyzed, failed, remaining)
    return {"analyzed": analyzed, "failed": failed, "remaining": remaining}


# ── Parsing & validation (unit-tested directly) ──────────────────────


def parse_analysis(
    raw: str, active_slugs: set[str], is_competitor_item: bool
) -> AnalysisResult:
    """Strict parse of the model's JSON. Raises AnalysisError on anything
    off — the caller marks the item for re-analysis."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise AnalysisError(f"Geen JSON-object in het antwoord: {raw[:120]!r}")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise AnalysisError(f"JSON niet leesbaar: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError("Antwoord is geen JSON-object.")

    sentiment = _enum(data.get("sentiment"), _SENTIMENTS, "sentiment")
    type_ = _enum(data.get("type"), _TYPES, "type")
    urgency = _enum(data.get("urgency"), _URGENCIES, "urgency")

    raw_themes = data.get("themes") or []
    if not isinstance(raw_themes, list):
        raise AnalysisError("'themes' is geen lijst.")
    themes = [str(t).strip() for t in raw_themes if str(t).strip() in active_slugs]
    if not themes:
        themes = [FALLBACK_THEME] if FALLBACK_THEME in active_slugs else []

    raw_flag = data.get("health_flag")
    if isinstance(raw_flag, str):  # the model may answer "true"/"false" as text
        raw_flag = raw_flag.strip().casefold() in ("true", "ja", "yes", "1")
    health_flag = bool(raw_flag) or HEALTH_THEME in themes
    if health_flag:
        urgency = "urgent"
        if HEALTH_THEME in active_slugs and HEALTH_THEME not in themes:
            themes.insert(0, HEALTH_THEME)

    try:
        confidence = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.5

    language = data.get("language")
    language = str(language).strip().casefold()[:8] if language else None

    def _text_or_none(key: str) -> str | None:
        value = data.get(key)
        if not is_competitor_item or value is None:
            return None
        value = str(value).strip()
        return value[:300] or None

    return AnalysisResult(
        sentiment=sentiment,
        themes=themes,
        urgency=urgency,
        type=type_,
        health_flag=health_flag,
        confidence=confidence,
        language=language,
        competitor_pro=_text_or_none("competitor_pro"),
        competitor_con=_text_or_none("competitor_con"),
    )


def _enum(value, allowed: set[str], field: str) -> str:
    normalized = str(value or "").strip().casefold()
    normalized = _SYNONYMS.get(normalized, normalized)
    if normalized not in allowed:
        raise AnalysisError(f"Ongeldige waarde voor {field}: {value!r}")
    return normalized


# ── Analyzers ────────────────────────────────────────────────────────


class Analyzer:
    """Interface: analyze one queue row into an AnalysisResult."""

    model_name = "onbekend"

    def analyze(self, item: sqlite3.Row, themes: list[sqlite3.Row]) -> AnalysisResult:
        raise NotImplementedError


class ClaudeAnalyzer(Analyzer):
    """The real one. Requires ANTHROPIC_API_KEY (see .env.example)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise AnalyzerUnavailable(
                "Geen ANTHROPIC_API_KEY in .env — analyse staat uit. Verzamelen "
                "werkt gewoon door; items wachten in de analyse-wachtrij. "
                "Sleutel aanmaken: zie PULSE.md, sectie 'AI-analyse aanzetten'."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise AnalyzerUnavailable(
                "Het 'anthropic'-pakket is niet geïnstalleerd. "
                "Installeer met: pip install -r requirements-ai.txt"
            ) from exc
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model_name = settings.anthropic_model

    def analyze(self, item: sqlite3.Row, themes: list[sqlite3.Row]) -> AnalysisResult:
        prompt = build_user_prompt(
            item["text"], item["source_name"], themes, item["competitor_name"]
        )
        resp = self._client.messages.create(
            model=self.model_name,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return parse_analysis(
            raw,
            active_slugs={t["slug"] for t in themes},
            is_competitor_item=item["competitor_name"] is not None,
        )


class CannedAnalyzer(Analyzer):
    """Offline analyzer for the demo: canned results per external_id from
    tests/fixtures/pulse/analyses_canned.json, with a keyword fallback so
    unknown items never break the demo."""

    model_name = "demo (vooraf ingevuld)"

    def __init__(self, fixture_dir: Path) -> None:
        path = Path(fixture_dir) / "analyses_canned.json"
        self._canned: dict[str, dict] = {}
        if path.exists():
            self._canned = json.loads(path.read_text(encoding="utf-8"))

    def analyze(self, item: sqlite3.Row, themes: list[sqlite3.Row]) -> AnalysisResult:
        active = {t["slug"] for t in themes}
        canned = self._canned.get(item["external_id"])
        if canned:
            return parse_analysis(
                json.dumps(canned),
                active_slugs=active,
                is_competitor_item=item["competitor_name"] is not None,
            )
        return self._heuristic(item, active)

    @staticmethod
    def _heuristic(item: sqlite3.Row, active: set[str]) -> AnalysisResult:
        from pulse import health

        text = item["text"].casefold()
        is_health = bool(item["pre_health_flag"]) or health.health_screen(item["text"])
        negative = any(w in text for w in ("slecht", "teleurgesteld", "niet gekregen", "klacht"))
        positive = any(w in text for w in ("super", "geweldig", "top", "aanrader", "blij"))
        return AnalysisResult(
            sentiment="negative" if (is_health or negative) else ("positive" if positive else "neutral"),
            themes=[HEALTH_THEME] if (is_health and HEALTH_THEME in active) else (
                [FALLBACK_THEME] if FALLBACK_THEME in active else []
            ),
            urgency="urgent" if is_health else "normal",
            type="complaint" if (is_health or negative) else ("compliment" if positive else "review"),
            health_flag=is_health,
            confidence=0.3,
        )
