"""report + notify: weekly markdown content (exact Dutch-formatted numbers),
the three takeaway sentences, empty-DB friendliness and the HTML twin."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import RUN_DAY

from compass import notify, report, store
from compass.models import (
    AMSTERDAM,
    DISCLAIMER,
    NO_BOOKKEEPING,
    AdSpendRecord,
    CostModel,
    InventoryRecord,
    OrderRecord,
    PaymentFee,
    Signal,
)

# Report windows around RUN_DAY (2026-07-01): this week 24–30 June, the
# comparison week 17–23 June. All seeded orders sit inside those windows.
THIS_WEEK_START = date(2026, 6, 24)


def model() -> CostModel:
    """The reference cost model (same numbers as the metrics tests)."""
    return CostModel(
        valid_from=date(2026, 6, 1),
        cogs_per_unit_cents=650,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=0.09,
        fixed_month_cents=40_000,
        lead_time_days=30,
        safety_factor=1.3,
        note="v1",
    )


def order(
    extern_id: str,
    day: date,
    *,
    channel: str = "shopify",
    gross: int = 2995,
    net: int | None = 2748,
    payment: str | None = "ideal",
    hash_: str | None = None,
) -> OrderRecord:
    return OrderRecord(
        extern_id=extern_id,
        channel=channel,
        ordered_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=AMSTERDAM),
        gross_cents=gross,
        net_cents=net,
        vat_cents=None if net is None else gross - net,
        customer_hash=hash_,
        payment_method=payment,
        raw={"id": extern_id},
    )


def spend(day: date, cents: int, purchases: int, value_cents: int) -> AdSpendRecord:
    return AdSpendRecord(
        day=day,
        campaign_id="c1",
        campaign_name="Prospecting",
        spend_cents=cents,
        impressions=10_000,
        clicks=150,
        meta_purchases=purchases,
        meta_purchase_value_cents=value_cents,
        raw={},
    )


@pytest.fixture
def seeded_report_db(compass_db):
    """Two seeded weeks with hand-checked totals.

    Last week (17–23 June): 20 Shopify orders (€29,95 each, iDEAL, all new
    customers), €400 spend. This week (24–30 June): 35 Shopify + 5 bol
    orders, €500 spend, Meta claiming 30 purchases worth € 898,50 against
    € 1.198,00 actual revenue. Plus a stock snapshot of 500 units.
    """
    conn = compass_db
    store.add_cost_model(conn, model())

    n = 0
    for day, count in ((date(2026, 6, 18), 10), (date(2026, 6, 20), 10)):
        for _ in range(count):
            n += 1
            store.upsert_order(conn, order(f"S{n}", day, hash_=f"klant-{n}"))
    for offset in range(7):  # 5 Shopify orders per day, 24–30 June
        day = THIS_WEEK_START + timedelta(days=offset)
        for _ in range(5):
            n += 1
            store.upsert_order(conn, order(f"S{n}", day, hash_=f"klant-{n}"))
    for i in range(5):  # bol masks identity: no hash, no net split
        store.upsert_order(
            conn, order(f"B{i}", date(2026, 6, 25), channel="bol", net=None, payment=None)
        )

    store.upsert_ad_spend(conn, spend(date(2026, 6, 19), 40_000, 10, 29_950))
    store.upsert_ad_spend(conn, spend(date(2026, 6, 25), 30_000, 18, 53_910))
    store.upsert_ad_spend(conn, spend(date(2026, 6, 27), 20_000, 12, 35_940))
    store.upsert_inventory(
        conn, InventoryRecord(day=date(2026, 6, 30), units=500, source="shopify")
    )
    store.rebuild_daily_metrics(conn, date(2026, 6, 1), RUN_DAY)
    return conn


# ── kerncijfers ──────────────────────────────────────────────────────


def test_weekly_markdown_header_and_period_use_dutch_dates(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)
    assert md.startswith("# Compass weekrapport — week tot 01-07-2026")
    assert "Periode: 24-06-2026 t/m 30-06-2026" in md
    assert "17-06-2026 t/m 23-06-2026" in md


def test_weekly_markdown_kerncijfers_show_exact_dutch_numbers(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)

    assert "| Kerncijfer | Deze week | Vorige week | Verschil |" in md
    assert "| Omzet totaal (incl. btw) | € 1.198,00 | € 599,00 | +100,0% |" in md
    assert "| Omzet Shopify (incl. btw) | € 1.048,25 | € 599,00 | +75,0% |" in md
    assert "| Omzet bol (incl. btw) | € 149,75 | € 0,00 | — |" in md
    assert "| Orders | 40 | 20 | +100,0% |" in md
    assert "| AOV (gemiddelde orderwaarde) | € 29,95 | € 29,95 | 0,0% |" in md
    assert "| Ad spend | € 500,00 | € 400,00 | +25,0% |" in md
    assert "| MER (omzet excl. btw / spend) | 2,20 | 1,37 | +60,0% |" in md
    assert "| Break-even ROAS | 1,73 | 1,69 | +2,7% |" in md
    assert "| Contributiemarge | € 634,50 | € 325,80 | +94,8% |" in md
    assert "| Nieuwe klanten | 40 | 20 | +100,0% |" in md
    assert "| CAC (kosten per nieuwe klant) | € 12,50 | € 20,00 | -37,5% |" in md


# ── volgens Meta vs werkelijk ────────────────────────────────────────


def test_weekly_markdown_compares_meta_claims_with_actual_revenue(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)

    assert "## Volgens Meta vs werkelijk" in md
    assert "Aankopen volgens Meta: 30 — werkelijke orders (Shopify + bol): 40" in md
    assert (
        "Aankoopwaarde volgens Meta: € 898,50 — werkelijke omzet (incl. btw): "
        "€ 1.198,00"
    ) in md
    assert (
        "Verschil: € 299,50 — Meta rapporteert 25,0% minder dan de werkelijke omzet."
    ) in md


# ── signals ──────────────────────────────────────────────────────────


def test_weekly_markdown_without_signals_says_so(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)
    assert "## Actieve signalen" in md
    assert "Geen actieve signalen." in md


def test_weekly_markdown_lists_active_signals_with_dutch_labels(seeded_report_db):
    conn = seeded_report_db
    store.upsert_signal(
        conn,
        Signal(
            type="reorder",
            day=RUN_DAY,
            message=(
                "Bestel nieuwe voorraad; bij de huidige verkoopsnelheid ben "
                "je over 33 dagen uitverkocht (rond 03-08-2026)."
            ),
            explanation=(
                "Voorraad: 400 stuks.\n"
                "De advertentiecijfers zijn goed genoeg om op te schalen, "
                "maar bestel eerst voorraad bij."
            ),
        ),
    )
    conn.commit()

    md = report.build_weekly_markdown(conn, today=RUN_DAY)
    assert "**Voorraad bestellen** — 01-07-2026: Bestel nieuwe voorraad;" in md
    assert "> Voorraad: 400 stuks." in md
    assert "> De advertentiecijfers zijn goed genoeg om op te schalen" in md
    assert "Geen actieve signalen." not in md


def test_weekly_markdown_skips_seen_signals(seeded_report_db):
    conn = seeded_report_db
    store.upsert_signal(
        conn, Signal(type="scale_up", day=RUN_DAY, message="Overweeg opschalen.")
    )
    conn.commit()
    row = conn.execute("SELECT id FROM signals WHERE type = 'scale_up'").fetchone()
    store.mark_signal_seen(conn, row["id"])

    md = report.build_weekly_markdown(conn, today=RUN_DAY)
    assert "Overweeg opschalen." not in md
    assert "Geen actieve signalen." in md


# ── voorraad ─────────────────────────────────────────────────────────


def test_weekly_markdown_voorraad_section_shows_stock_and_reorder_point(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)

    assert "## Voorraad" in md
    assert "- Voorraad: 500 stuks (bron: shopify, peildatum 30-06-2026)" in md
    assert "- Verkoopsnelheid (gewogen, 30 dagen): 3,1 per dag" in md
    assert "- Dagen voorraad: ongeveer 160" in md
    # reorder point: 30 dagen levertijd × 1,3 veiligheidsfactor = 39 dagen
    assert "- Bestelpunt: 122 stuks (39 dagen voorraad)" in md
    assert "- Verwachte uitverkoopdatum: rond 08-12-2026" in md


def test_weekly_markdown_without_inventory_asks_for_a_count(seeded_report_db):
    conn = seeded_report_db
    conn.execute("DELETE FROM inventory_snapshots")
    conn.commit()

    md = report.build_weekly_markdown(conn, today=RUN_DAY)
    assert "Nog geen voorraadstand bekend" in md
    assert "Er is nog geen voorraadstand bekend" in md  # takeaway sentence


# ── wat betekent dit ─────────────────────────────────────────────────


def _takeaway_bullets(md: str) -> list[str]:
    section = md.split("## Wat betekent dit", 1)[1].split("---", 1)[0]
    return [line for line in section.splitlines() if line.startswith("- ")]


def test_weekly_markdown_wat_betekent_dit_has_exactly_three_sentences(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)
    bullets = _takeaway_bullets(md)
    assert len(bullets) == 3
    assert bullets[0] == (
        "- De advertenties verdienden zichzelf deze week terug: elke euro "
        "spend leverde 2,20 euro omzet excl. btw op, en 1,73 is genoeg om "
        "quitte te spelen."
    )
    assert bullets[1] == "- De omzet steeg met 100,0% ten opzichte van vorige week."
    assert bullets[2] == (
        "- De voorraad is ruim voldoende: nog ongeveer 160 dagen bij het "
        "huidige verkooptempo."
    )


def test_takeaway_sentences_flip_below_break_even_and_on_falling_revenue(compass_db):
    conn = compass_db
    store.add_cost_model(conn, model())
    # Big prior week, small loss-making current week under heavy spend.
    for i in range(10):
        store.upsert_order(conn, order(f"P{i}", date(2026, 6, 18), hash_=f"h-{i}"))
    store.upsert_order(conn, order("N1", date(2026, 6, 26), hash_="h-nieuw"))
    store.upsert_ad_spend(conn, spend(date(2026, 6, 26), 20_000, 1, 2995))
    store.rebuild_daily_metrics(conn, date(2026, 6, 15), RUN_DAY)

    md = report.build_weekly_markdown(conn, today=RUN_DAY)
    bullets = _takeaway_bullets(md)
    assert len(bullets) == 3
    assert "onder break-even" in bullets[0]
    assert "De omzet daalde met 90,0% ten opzichte van vorige week." == bullets[1][2:]


# ── empty database ───────────────────────────────────────────────────


def test_weekly_markdown_on_empty_db_is_friendly(compass_db):
    md = report.build_weekly_markdown(compass_db, today=RUN_DAY)
    assert md.startswith("# Compass weekrapport — week tot 01-07-2026")
    assert "nog geen data" in md
    assert "compass collect" in md
    assert DISCLAIMER in md
    assert NO_BOOKKEEPING in md


def test_write_weekly_on_empty_db_still_writes_both_files(compass_db, tmp_path):
    md_path, html_path = report.write_weekly(compass_db, tmp_path / "reports", today=RUN_DAY)
    assert md_path.is_file() and html_path.is_file()
    assert "nog geen data" in md_path.read_text(encoding="utf-8")


# ── footer ───────────────────────────────────────────────────────────


def test_weekly_markdown_footer_carries_both_disclaimers(seeded_report_db):
    md = report.build_weekly_markdown(seeded_report_db, today=RUN_DAY)
    assert DISCLAIMER in md
    assert NO_BOOKKEEPING in md


# ── html twin ────────────────────────────────────────────────────────


def test_write_weekly_writes_markdown_and_html_twin(seeded_report_db, tmp_path):
    reports_dir = tmp_path / "reports"
    md_path, html_path = report.write_weekly(seeded_report_db, reports_dir, today=RUN_DAY)

    assert md_path == reports_dir / "compass-week-2026-07-01.md"
    assert html_path == reports_dir / "compass-week-2026-07-01.html"
    assert md_path.is_file() and html_path.is_file()

    html_text = html_path.read_text(encoding="utf-8")
    # The markdown table must be converted: no raw pipes or separator rows left.
    assert "|---" not in html_text
    assert not [
        line for line in html_text.splitlines() if line.lstrip().startswith("|")
    ]
    assert "<table>" in html_text
    assert "<th>Kerncijfer</th>" in html_text
    assert "<td>€ 1.198,00</td>" in html_text
    assert "<h1>Compass weekrapport — week tot 01-07-2026</h1>" in html_text
    assert "<title>Compass weekrapport</title>" in html_text


# ── notify ───────────────────────────────────────────────────────────


def test_get_notifier_returns_null_notifier_that_logs_the_paths(caplog):
    notifier = notify.get_notifier()
    assert isinstance(notifier, notify.NullNotifier)
    with caplog.at_level(logging.INFO, logger="compass.notify"):
        notifier.send_report(
            "Compass weekrapport", "# rapport", [Path("reports/compass-week-2026-07-01.md")]
        )
    assert "Rapport staat klaar" in caplog.text
    assert "compass-week-2026-07-01.md" in caplog.text
