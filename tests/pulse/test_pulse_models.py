"""Pure model helpers: hashing and dedupe keys (privacy-critical)."""

from __future__ import annotations

from pulse.models import author_hash, content_hash


def test_author_hash_normalizes_case_and_whitespace():
    assert author_hash("Marijke@Ziggo.nl ") == author_hash("marijke@ziggo.nl")


def test_author_hash_never_contains_the_address():
    h = author_hash("marijke.deboer@ziggo.nl")
    assert h is not None and len(h) == 64
    assert "marijke" not in h and "@" not in h


def test_author_hash_empty_is_none():
    assert author_hash(None) is None
    assert author_hash("   ") is None


def test_content_hash_collapses_whitespace():
    a = content_hash("Werkt  super!\n", "Els", "2026-06-01")
    b = content_hash("Werkt super!", "els ", "2026-06-01T10:00:00")
    assert a == b


def test_content_hash_differs_per_author_and_date():
    base = content_hash("Werkt super!", "Els", "2026-06-01")
    assert content_hash("Werkt super!", "Ans", "2026-06-01") != base
    assert content_hash("Werkt super!", "Els", "2026-06-02") != base
