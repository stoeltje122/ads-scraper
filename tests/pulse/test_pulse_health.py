"""The health keyword screen: broad on real signals, quiet on daily talk."""

from __future__ import annotations

import pytest

from pulse.health import health_screen, matched_keywords


@pytest.mark.parametrize(
    "text",
    [
        "Ik heb last van hartkloppingen sinds ik dit gebruik",
        "Sinds gisteren hoofdpijnklachten, kan dat hierdoor komen?",
        "Ik ben Misselijk geworden van de capsules",
        "Mag ik dit gebruiken tijdens de zwangerschap?",
        "Kan dit samen met bloedverdunners?",
        "I got a headache after two days",
        "is this safe while pregnant?",
        "kreeg er huiduitslag van op mijn armen",
        "mijn huisarts raadde het af vanwege mijn schildklier",
        "na drie dagen buikpijn gekregen en gestopt",
    ],
)
def test_flags_possible_health_signals(text):
    assert health_screen(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "De levering duurde vijf dagen, dat kan sneller",  # 'lever' ambiguity
        "Snel geleverd en netjes verpakt, top!",
        "De vernieuwde verpakking vind ik mooi",           # 'nier' ambiguity
        "Mijn bestelling crashte in de checkout",           # 'rash' needs word bounds
        "Zit er melatonine in dit product?",
        "Werkt echt goed, ik slaap eindelijk door!",
        "",
    ],
)
def test_does_not_flag_ordinary_feedback(text):
    assert health_screen(text) is False


def test_matched_keywords_explains_the_flag():
    hits = matched_keywords("Hoofdpijn én misselijk geworden")
    assert "hoofdpijn" in hits and any("misselijk" in h for h in hits)


def test_accents_and_case_are_normalized():
    assert health_screen("MISSELĲK geworden") is True
