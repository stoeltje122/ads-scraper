"""report: weekly markdown content and the HTML twin."""

from __future__ import annotations

from datetime import date

from conftest import RUN_DAY

from adscout import report


def _days(start: str, end: date) -> int:
    return (end - date.fromisoformat(start)).days


def test_weekly_markdown_contains_per_advertiser_table_rows(collected_db):
    conn, _ = collected_db
    md = report.build_weekly_markdown(conn, today=RUN_DAY)

    assert f"# AdScout weekrapport — {RUN_DAY.isoformat()}" in md
    assert "## Per concurrent" in md
    # advertiser | category | active now | new (7d) | stopped (7d) | total
    assert "| 8hours | supplement | 1 | 1 | 0 | 1 |" in md
    assert "| Cloudpillo | sleep-comfort | 2 | 3 | 0 | 3 |" in md
    assert "| Zelesta | sleep-comfort | 1 | 2 | 1 | 2 |" in md


def test_weekly_markdown_top_runners_section(collected_db):
    conn, _ = collected_db
    md = report.build_weekly_markdown(conn, today=RUN_DAY)

    assert "## Top 5 langstlopers per categorie (actieve ads)" in md
    assert "### sleep-comfort" in md
    assert "### supplement" in md
    # Longest runner overall: Cloudpillo's ad running since 2025-11-06.
    longest = _days("2025-11-06", RUN_DAY)
    assert (
        f"- **Cloudpillo** — {longest} dagen, unknown, gestart 2025-11-06 — "
        "[Ad Library](https://www.facebook.com/ads/library/?id=1001001001003)"
    ) in md


def test_weekly_markdown_new_and_stopped_sections(collected_db):
    conn, _ = collected_db
    md = report.build_weekly_markdown(conn, today=RUN_DAY)

    # All 6 fixture ads were first seen inside the report window.
    assert "## Nieuw deze week (6)" in md
    # Exactly one ad stopped inside the window: Zelesta's 2026-06-25 stop.
    assert "## Gestopt deze week (1)" in md
    ran = _days("2026-06-01", date(2026, 6, 25))
    assert (
        f"- Zelesta: liep {ran} dagen — "
        "[Ad Library](https://www.facebook.com/ads/library/?id=2002002002002)"
    ) in md
    assert "*Geen gestopte ads gezien.*" not in md
    assert "*Geen nieuwe ads gezien.*" not in md


def test_weekly_markdown_on_empty_db_says_so(tmp_db):
    md = report.build_weekly_markdown(tmp_db, today=RUN_DAY)
    assert "*Geen nieuwe ads gezien.*" in md
    assert "*Geen gestopte ads gezien.*" in md


def test_write_weekly_writes_markdown_and_html(collected_db, tmp_path):
    conn, _ = collected_db
    reports_dir = tmp_path / "reports"

    md_path, html_path = report.write_weekly(conn, reports_dir, today=RUN_DAY)

    assert md_path == reports_dir / f"weekly-{RUN_DAY.isoformat()}.md"
    assert html_path == reports_dir / f"weekly-{RUN_DAY.isoformat()}.html"
    assert md_path.is_file() and html_path.is_file()

    html_text = html_path.read_text(encoding="utf-8")
    # The markdown table must be converted: no raw pipes or separator rows left.
    assert "|---" not in html_text
    assert not [
        line for line in html_text.splitlines() if line.lstrip().startswith("|")
    ]
    assert "<table>" in html_text
    assert "<th>Concurrent</th>" in html_text
    assert "<td>Cloudpillo</td>" in html_text
    assert f"<h1>AdScout weekrapport — {RUN_DAY.isoformat()}</h1>" in html_text


def test_ad_library_url_is_durable_public_link():
    assert report.ad_library_url("42") == "https://www.facebook.com/ads/library/?id=42"
