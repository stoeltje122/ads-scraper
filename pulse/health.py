"""Keyword pre-screen for possible health signals.

This is the safety net UNDER the AI analysis, not a replacement: it runs at
store time, needs no API key, and errs on the side of flagging. A flagged
item shows up in the Urgent view immediately — even while it is still
waiting in the analysis queue — and it STAYS there until a human marks it
'opgevolgd', even when the AI later judges it not health-related: a keyword
hit plus an AI 'no' is by definition doubt, and doubt means a human looks.
The matched keywords stay visible on the item detail page so the founders
can always see why something was pre-flagged.

Pulse never gives medical advice and never diagnoses; it only marks items
for human follow-up.
"""

from __future__ import annotations

import re
import unicodedata

# Dutch + English signal words, matched as word *prefixes* ("hoofdpijn" also
# hits "hoofdpijnklachten"). Deliberately broad: a false positive costs a
# founder one glance, a false negative could hide a real side-effect report.
# Ambiguous roots (e.g. "lever" = liver AND delivery) are NOT in this list;
# their unambiguous compounds are listed explicitly in _EXACT_WORDS.
_PREFIXES = [
    # side effects / physical complaints
    "bijwerking", "side effect", "side-effect",
    "hoofdpijn", "headache", "migraine",
    "misselijk", "nausea", "overgeef", "overgegeven", "braken", "gebraakt", "vomit",
    "duizelig", "dizzy", "dizziness",
    "hartklopping", "palpitation", "hartritme",
    "huiduitslag", "galbulten", "netelroos", "jeuk",
    "allergi", "anafyla",
    "benauwd", "kortademig",
    "diarree", "diarrhea", "buikpijn", "buikkramp", "maagpijn", "maagklacht", "maagkramp",
    "stomach ache", "stomach pain",
    "tremor", "trillende handen",
    "nachtmerrie", "nightmare", "hallucin",
    "verslaafd", "verslaving", "verslavend", "addict", "afhankelijk geworden",
    "ziek geworden", "ziek van", "onwel", "flauwgevallen", "flauwvallen", "fainted",
    "opgezwollen", "gezwollen", "zwelling", "swelling", "swollen",
    "pijn op de borst", "chest pain",
    "paniekaanval", "panic attack", "angstaanval",
    # medication interaction / medical care
    "medicijn", "medicatie", "medication", "wisselwerking", "interactie",
    "antidepressiv", "bloedverdunner", "bloeddruk", "antistolling",
    "apothe", "huisarts", "dokter", "pharmacist",
    "ziekenhuis", "spoedeisende", "hospitalized", "hospitalised",
    # pregnancy / breastfeeding
    "zwanger", "pregnan", "borstvoeding", "breastfeed", "kinderwens", "vruchtbaarheid",
    # vulnerable groups / conditions
    "epilep", "diabet", "schildklier",
    "leverklacht", "leverschade", "leverwaarde", "leverfunctie",
    "nierklacht", "nierschade", "nierfunctie", "nierproble",
]

# Short/ambiguous terms that need an exact word match ("rash" would
# otherwise hit "crash", "hives" would hit "archives").
_EXACT_WORDS = ["rash", "hives", "itch", "itchy", "hospital", "arts", "uitslag"]

_PATTERN = re.compile(
    "|".join(
        [r"\b(?:%s)" % "|".join(re.escape(k) for k in _PREFIXES)]
        + [r"\b(?:%s)\b" % "|".join(re.escape(k) for k in _EXACT_WORDS)]
    )
)


def normalize(text: str) -> str:
    """Casefold and strip accents so 'Misselijk' and 'misselĳk' both hit."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def health_screen(text: str) -> bool:
    """True when the text *could* point at a health issue. Doubt = flag."""
    if not text:
        return False
    return _PATTERN.search(normalize(text)) is not None


def matched_keywords(text: str) -> list[str]:
    """The distinct matches that hit — shown on the item detail page so the
    founders see *why* something was pre-flagged."""
    if not text:
        return []
    return sorted(set(_PATTERN.findall(normalize(text))))
