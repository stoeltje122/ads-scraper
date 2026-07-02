"""Gmail adapter: parsing, filtering and quote-stripping on fixtures."""

from __future__ import annotations

import pytest

from helpers_pulse import PULSE_FIXTURE_DIR
from pulse.sources.gmail import GmailAdapter, extract_body_text, strip_quoted_reply


@pytest.fixture
def adapter(pulse_settings) -> GmailAdapter:
    return GmailAdapter(pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)


def test_fixture_collect_filters_newsletters_and_own_mail(adapter):
    items = adapter.collect(since=None)
    ids = {i.external_id for i in items}
    assert "msg-news-01" not in ids     # List-Unsubscribe → filtered
    assert "msg-reply-01" not in ids    # from info@cloudplunge.com → filtered
    assert "msg-health-01" in ids
    assert len(items) == 10


def test_subject_is_prepended_and_thread_linked(adapter):
    by_id = {i.external_id: i for i in adapter.collect(since=None)}
    health = by_id["msg-health-01"]
    assert health.text.startswith("Hartkloppingen na inname")
    assert health.thread_external_id == "thread-health-01"
    assert health.author_ref == "marijke.deboer@ziggo.nl"
    assert health.author_display == "Marijke de Boer"
    assert health.happened_at.startswith("2026-06-30")


def test_raw_json_contains_no_address(adapter):
    for item in adapter.collect(since=None):
        assert "@" not in item.raw_json()


def test_quoted_reply_is_stripped(adapter):
    by_id = {i.external_id: i for i in adapter.collect(since=None)}
    text = by_id["msg-quote-01"].text
    assert "kortingscode" in text.casefold()
    assert "schreef Cloudplunge" not in text  # quote marker line cut
    assert "natuurlijke formule" not in text  # quoted body cut


def test_html_fallback_extraction(adapter):
    by_id = {i.external_id: i for i in adapter.collect(since=None)}
    text = by_id["msg-html-01"].text
    assert "erg groot" in text
    assert "<" not in text


def test_since_filter(adapter):
    from datetime import datetime, timezone

    since = datetime(2026, 6, 29, tzinfo=timezone.utc)
    items = adapter.collect(since=since)
    assert {i.external_id for i in items} == {
        "msg-health-01", "msg-mel-01", "msg-lev-02", "msg-med-01", "msg-quote-01"
    }


def test_verify_fixture_mode(adapter):
    result = adapter.verify()
    assert result.ok and "12" in result.message


def test_verify_without_credentials(pulse_settings):
    result = GmailAdapter(pulse_settings).verify()
    assert not result.ok
    assert "credentials" in result.message.casefold()


def test_strip_quoted_reply_variants():
    text = "Nieuw antwoord\n\nOn Mon, Jun 29, 2026 John wrote:\n> oud\n> ouder"
    assert strip_quoted_reply(text) == "Nieuw antwoord"
    text2 = "Prima!\n-- \nHandtekening\nTelefoon"
    assert "Handtekening" not in strip_quoted_reply(text2)


def test_extract_body_prefers_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PGI+aHRtbDwvYj4"}},
            {"mimeType": "text/plain", "body": {"data": "cGxhaW4"}},
        ],
    }
    assert extract_body_text(payload) == "plain"


def test_forward_header_block_is_cut_but_inline_van_is_not():
    """Regression: 'Van:' only counts as quote header in a real header block."""
    forwarded = (
        "Zie onderstaande klacht.\n\n"
        "Van: Klant <k@x.nl>\nVerzonden: maandag\nAan: info@cloudplunge.com\n"
        "Onderwerp: klacht\n\nHele oude tekst"
    )
    assert strip_quoted_reply(forwarded) == "Zie onderstaande klacht."

    inline = "Van: de webshop kreeg ik geen antwoord.\nDaarom mail ik jullie nu."
    assert "Daarom mail ik jullie nu." in strip_quoted_reply(inline)


def test_fully_quoted_mail_falls_back_to_original():
    """A customer's words are never silently discarded."""
    only_forward = (
        "Van: Klant <k@x.nl>\nVerzonden: maandag\nAan: ons\n\n"
        "Ik ben erg ontevreden over mijn bestelling."
    )
    result = strip_quoted_reply(only_forward)
    assert "ontevreden" in result


def test_author_display_never_falls_back_to_address(pulse_settings):
    adapter = GmailAdapter(pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    msg = adapter._fixture_messages()[0]
    for header in msg["payload"]["headers"]:
        if header["name"] == "From":
            header["value"] = "naamloos@voorbeeld.nl"  # no display name
    item = adapter.parse_message(msg)
    assert item.author_display is None
    assert item.author_ref == "naamloos@voorbeeld.nl"
