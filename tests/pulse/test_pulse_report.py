"""Weekly report: content, files, digest bookkeeping."""

from __future__ import annotations

from helpers_pulse import RUN_DAY
from pulse import report, store


def test_write_weekly_produces_md_and_html(pulse_demo_db, tmp_path):
    md_path, html_path = report.write_weekly(pulse_demo_db, tmp_path / "reports", today=RUN_DAY)
    assert md_path.exists() and html_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert "GEZONDHEID" in md
    assert "Hartkloppingen" in md
    assert "Top-klachten" in md
    assert "Aanbevolen acties" in md
    assert "8hours" in md  # competitor kansen
    html = html_path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "<h2>" in html

    digest = pulse_demo_db.execute("SELECT * FROM digests").fetchone()
    assert digest["period_end"] == RUN_DAY.isoformat()
    assert digest["md_path"] == str(md_path)


def test_report_reflects_followed_up_status(pulse_demo_db, tmp_path):
    for item in pulse_demo_db.execute(
        "SELECT i.id FROM items i JOIN analyses a ON a.item_id = i.id "
        "WHERE a.health_flag = 1 AND i.competitor_id IS NULL"
    ).fetchall():
        store.mark_followed_up(pulse_demo_db, item["id"])
    md_path, _ = report.write_weekly(pulse_demo_db, tmp_path / "r", today=RUN_DAY)
    md = md_path.read_text(encoding="utf-8")
    assert "opgevolgd ✓" in md


def test_html_escapes_content():
    html = report.render_html("# Kop\n- item met <script>alert(1)</script>\n")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_actions_mention_queue(pulse_seeded_db, pulse_settings):
    from pulse import collector

    from helpers_pulse import PULSE_FIXTURE_DIR

    collector.collect(pulse_seeded_db, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    data = report.collect_week_data(pulse_seeded_db, today=RUN_DAY)
    assert any("pulse analyze" in a for a in data["actions"])
