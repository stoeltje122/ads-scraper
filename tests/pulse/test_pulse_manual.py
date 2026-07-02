"""Manual import: CSV parsing, paste parsing, channel routing, dedupe."""

from __future__ import annotations

import pytest

from pulse.sources.base import SourceError
from pulse.sources.manual import channel_to_source_type, parse_csv, parse_paste


def test_parse_csv_with_dutch_headers():
    content = (
        "kanaal,concurrent,datum,sterren,titel,tekst,auteur,url\n"
        'trustpilot,Cloudpillo,2026-06-27,2,Kussen zakt in,"Na twee maanden ingezakt.",Renate B.,https://x\n'
    )
    items = parse_csv(content)
    assert len(items) == 1
    item = items[0]
    assert item.competitor_name == "Cloudpillo"
    assert item.happened_at == "2026-06-27"
    assert item.text.startswith("[2/5 sterren] Kussen zakt in")
    assert item.raw["kanaal"] == "trustpilot"
    assert item.author_display == "Renate B."


def test_parse_csv_semicolon_and_english_headers():
    content = "date;text;author\n28-06-2026;Great product!;Jane\n"
    items = parse_csv(content)
    assert items[0].happened_at == "2026-06-28"
    assert items[0].text == "Great product!"


def test_parse_csv_without_text_column_fails_clearly():
    with pytest.raises(SourceError, match="tekstkolom"):
        parse_csv("datum,auteur\n2026-01-01,Jan\n")


def test_parse_csv_skips_empty_rows_but_needs_one():
    content = "tekst\n\n\n"
    with pytest.raises(SourceError, match="Geen rijen"):
        parse_csv(content)


def test_parse_paste_splits_on_dashes():
    items = parse_paste("Eerste review\n---\nTweede review", competitor="8hours",
                        channel_label="bol", date="2026-06-28")
    assert len(items) == 2
    assert all(i.competitor_name == "8hours" for i in items)
    assert all(i.happened_at == "2026-06-28" for i in items)
    assert all(i.raw["kanaal"] == "bol" for i in items)


def test_parse_paste_empty_fails():
    with pytest.raises(SourceError):
        parse_paste("   \n---\n  ")


def test_same_content_same_external_id():
    a = parse_paste("Zelfde review", competitor="X")[0]
    b = parse_paste("Zelfde  review\n", competitor="X")[0]
    assert a.external_id == b.external_id


def test_channel_routing():
    assert channel_to_source_type("Trustpilot") == "trustpilot"
    assert channel_to_source_type("bol.com") == "bol"
    assert channel_to_source_type("Instagram") == "meta_comments"
    assert channel_to_source_type("e-mail") == "gmail"
    assert channel_to_source_type("telefoon") == "manual"
    assert channel_to_source_type(None) == "manual"


def test_unknown_date_format_is_dropped_not_fatal():
    items = parse_csv("tekst,datum\nPrima product,ergens in juni\n")
    assert items[0].happened_at is None


def test_dangerous_url_schemes_are_dropped():
    """Regression (stored XSS): only http(s) URLs survive an import."""
    items = parse_csv(
        "tekst,url\n"
        "review a,javascript:alert(1)\n"
        "review b,https://nl.trustpilot.com/x\n"
        "review c,data:text/html;base64:x\n"
    )
    assert items[0].url is None
    assert items[1].url == "https://nl.trustpilot.com/x"
    assert items[2].url is None


def test_single_text_column_keeps_commas():
    """Regression: a one-column file must not be split on commas in the text."""
    items = parse_csv("tekst\nWerkt goed, snel geleverd, aanrader!\n")
    assert items[0].text == "Werkt goed, snel geleverd, aanrader!"
