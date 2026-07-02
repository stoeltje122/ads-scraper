"""Web dashboard: every route renders Dutch, POST flows mutate via store,
the demo flag targets the demo database and an empty DB still serves pages.

The dashboard windows everything on ams_today(), so this file seeds data
*relative to today* instead of the fixed RUN_DAY: the page structure and
all assertions are date-invariant that way, and the suite stays offline
and deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from compass import queries, signals, store
from compass.config import Settings
from compass.db import open_db
from compass.metrics import MarginBreakdown
from compass.models import (
    AMSTERDAM,
    AdSpendRecord,
    CostModel,
    InventoryRecord,
    OrderRecord,
    PaymentFee,
    Signal,
    ams_today,
)
from compass.web import chart
from compass.web.app import create_app

TODAY = ams_today()

ALL_ROUTES = (
    "/",
    "/maand",
    "/advertenties",
    "/economie",
    "/voorraad",
    "/signalen",
    "/instellingen",
)

SIGNAL_MESSAGE = "Bestel nieuwe voorraad; de teststand raakt op."


def order(
    extern_id: str,
    day: date,
    *,
    channel: str = "shopify",
    gross: int = 2995,
    net: int | None = 2748,
    units: int = 1,
    payment: str | None = "ideal",
    hash_: str | None = None,
) -> OrderRecord:
    return OrderRecord(
        extern_id=extern_id,
        channel=channel,
        ordered_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=AMSTERDAM),
        gross_cents=gross,
        units=units,
        net_cents=net,
        vat_cents=None if net is None else gross - net,
        customer_hash=hash_,
        payment_method=payment,
        raw={"id": extern_id},
    )


def cost_model(valid_from: date, **overrides) -> CostModel:
    values = dict(
        valid_from=valid_from,
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
        note="testmodel",
    )
    values.update(overrides)
    return CostModel(**values)


def seed_database(conn) -> None:
    """A small but complete world: orders on both channels, spend on two
    campaigns, inventory and one open signal — all through store.*"""
    store.add_cost_model(conn, cost_model(TODAY - timedelta(days=60)))
    for offset in range(1, 11):
        day = TODAY - timedelta(days=offset)
        store.upsert_order(conn, order(f"S-{offset}", day, hash_=f"klant-{offset}"))
        store.upsert_order(
            conn, order(f"B-{offset}", day, channel="bol", net=None, payment=None)
        )
    store.upsert_order(conn, order("S-vandaag", TODAY, hash_="klant-vandaag"))
    for offset in range(0, 11):
        day = TODAY - timedelta(days=offset)
        store.upsert_ad_spend(
            conn,
            AdSpendRecord(
                day=day, campaign_id="c1", campaign_name="Prospecting",
                spend_cents=4000, impressions=9000, clicks=140,
                meta_purchases=1, meta_purchase_value_cents=2995, raw={},
            ),
        )
        store.upsert_ad_spend(
            conn,
            AdSpendRecord(
                day=day, campaign_id="c2", campaign_name="Retargeting",
                spend_cents=1500, impressions=2500, clicks=60,
                meta_purchases=1, meta_purchase_value_cents=2995, raw={},
            ),
        )
    store.upsert_inventory(
        conn, InventoryRecord(day=TODAY - timedelta(days=1), units=900, source="shopify")
    )
    store.upsert_signal(
        conn,
        Signal(
            type="reorder",
            day=TODAY - timedelta(days=1),
            message=SIGNAL_MESSAGE,
            explanation="Voorraad: 900 stuks bij 2 per dag.",
            details={"units": 900},
        ),
    )
    store.rebuild_daily_metrics(conn, TODAY - timedelta(days=30), TODAY)
    conn.commit()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "compass-web")


@pytest.fixture
def seeded_settings(settings) -> Settings:
    conn = open_db(settings.db_path)
    seed_database(conn)
    conn.close()
    return settings


@pytest.fixture
def client(seeded_settings) -> TestClient:
    return TestClient(create_app(seeded_settings))


def read_db(settings: Settings):
    return open_db(settings.db_path)


# ── all routes render, in Dutch ──────────────────────────────────────


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_every_route_renders_on_a_seeded_database(client, route):
    resp = client.get(route)
    assert resp.status_code == 200
    assert '<html lang="nl">' in resp.text
    if route in ("/", "/maand", "/advertenties", "/economie", "/instellingen"):
        assert "€" in resp.text  # money pages format cents as Dutch euros


def test_home_shows_week_cards_meta_honesty_and_verdict(client):
    body = client.get("/").text
    # 7 shopify + 6 bol paid orders in the running week à €29,95.
    assert "€ 389,35" in body
    assert "volgens Meta" in body
    assert "break-even" in body.lower()
    assert "omzet excl. btw / spend" in body
    assert 'class="verdict verdict-' in body
    assert "verdict-none" not in body  # seeded week has spend and revenue
    assert SIGNAL_MESSAGE in body  # open signal is surfaced on the front page
    # Advisory surface carries the disclaimer.
    assert "geen financieel advies" in body


def test_home_warns_when_no_run_ever_happened(client):
    assert "Nog nooit een geslaagde datarun" in client.get("/").text


def test_maand_shows_cost_buildup_and_no_bookkeeping(client):
    body = client.get("/maand").text
    assert "Netto-indicatie" in body
    assert "geen boekhouding" in body
    assert "bol-commissie" in body


def test_advertenties_lists_campaigns_with_meta_labels(client):
    body = client.get("/advertenties").text
    assert "Prospecting" in body and "Retargeting" in body
    assert "volgens Meta" in body
    assert "attributieregels" in body  # the fixed honesty explainer
    # 11 days × €40,00 for the biggest campaign.
    # 10 complete days × €40 — today (partial day) stays out of the window.
    assert "€ 400,00" in body


def test_advertenties_dagen_selector_and_bad_values(client):
    assert client.get("/advertenties?dagen=7").status_code == 200
    assert client.get("/advertenties?dagen=999").status_code == 200  # falls back to 30
    assert client.get("/advertenties?dagen=abc").status_code == 200


def test_economie_shows_waterfall_numbers_and_explainer(client):
    body = client.get("/economie").text
    assert "Shopify (eigen shop)" in body
    assert "Contributiemarge" in body
    assert "Elke euro ad spend moet minstens" in body
    assert "break-even" in body.lower()


def test_voorraad_shows_stock_reorder_point_and_verdict(client):
    body = client.get("/voorraad").text
    assert "900" in body
    assert "Bestelpunt" in body
    assert "per dag" in body
    assert "uitverkoopdatum" in body.lower()


def test_signalen_lists_open_signal_with_dutch_label(client):
    body = client.get("/signalen").text
    assert SIGNAL_MESSAGE in body
    assert "Voorraad bestellen" in body  # signal_type_label('reorder')
    assert "Markeer als gezien" in body


# ── POST flows ───────────────────────────────────────────────────────


def test_mark_seen_flow_changes_status_and_clears_badge(client, seeded_settings):
    conn = read_db(seeded_settings)
    signal_id = queries.active_signals(conn)[0].id
    conn.close()

    resp = client.post(f"/signalen/{signal_id}/gezien", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signalen"

    conn = read_db(seeded_settings)
    assert queries.signal_by_id(conn, signal_id).status == "seen"
    assert queries.active_signals(conn) == []
    conn.close()
    body = client.get("/signalen").text
    assert "Gezien" in body


def test_mark_seen_unknown_signal_is_404(client):
    assert client.post("/signalen/99999/gezien").status_code == 404


def test_kostenmodel_post_creates_version_and_rebuilds_history(client, seeded_settings):
    conn = read_db(seeded_settings)
    probe_day = (TODAY - timedelta(days=2)).isoformat()
    margin_before = conn.execute(
        "SELECT margin_cents FROM daily_metrics WHERE day = ?", (probe_day,)
    ).fetchone()[0]
    conn.close()

    resp = client.post(
        "/instellingen/kostenmodel",
        data={
            "valid_from": (TODAY - timedelta(days=40)).isoformat(),
            "cogs_eur": "12,00",
            "shipping_eur": "4,40",
            "fee_pct": "1,5",
            "fee_fixed_eur": "0,25",
            "bol_commission_pct": "12,4",
            "vat_pct": "9",
            "fixed_month_eur": "400,00",
            "lead_time_days": "30",
            "safety_factor": "1,3",
            "note": "duurdere inkoop",
            "pf_method_1": "ideal",
            "pf_pct_1": "0",
            "pf_fixed_1": "0,29",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = read_db(seeded_settings)
    history = queries.cost_model_history(conn)
    assert len(history) == 2
    assert history[0].cogs_per_unit_cents == 1200
    assert history[0].payment_fees["ideal"].fixed_cents == 29
    # The rebuild re-priced history: €5,50 extra COGS per order lowers margins.
    margin_after = conn.execute(
        "SELECT margin_cents FROM daily_metrics WHERE day = ?", (probe_day,)
    ).fetchone()[0]
    conn.close()
    assert margin_after < margin_before
    assert "duurdere inkoop" in client.get("/instellingen").text


def test_kostenmodel_post_with_garbage_redirects_softly(client, seeded_settings):
    resp = client.post(
        "/instellingen/kostenmodel",
        data={"cogs_eur": "abc", "shipping_eur": "4,40"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "fout=kostenmodel" in resp.headers["location"]
    conn = read_db(seeded_settings)
    assert len(queries.cost_model_history(conn)) == 1  # nothing was saved
    conn.close()
    assert "controleer de ingevulde waarden" in client.get(resp.headers["location"]).text


def test_drempels_post_persists_and_feeds_get_thresholds(client, seeded_settings):
    resp = client.post(
        "/instellingen/drempels",
        data={"mer_scale_factor": "1,4", "min_spend_cents": "3000"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    conn = read_db(seeded_settings)
    assert queries.get_setting(conn, "signal.mer_scale_factor") == "1,4"
    thresholds = signals.get_thresholds(conn)
    conn.close()
    assert thresholds["mer_scale_factor"] == pytest.approx(1.4)
    assert thresholds["min_spend_cents"] == 3000
    assert 'value="1,4"' in client.get("/instellingen").text


def test_manual_inventory_post_wins_over_shopify(client, seeded_settings):
    resp = client.post(
        "/voorraad/handmatig", data={"units": "1.250"}, follow_redirects=False
    )
    assert resp.status_code == 303
    conn = read_db(seeded_settings)
    latest = queries.latest_inventory(conn)
    conn.close()
    assert latest.units == 1250
    assert latest.source == "manual"
    assert latest.day == TODAY


def test_manual_inventory_post_with_garbage_redirects_softly(client, seeded_settings):
    resp = client.post(
        "/voorraad/handmatig", data={"units": "veel"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "fout" in resp.headers["location"]
    conn = read_db(seeded_settings)
    assert queries.latest_inventory(conn).source == "shopify"  # unchanged
    conn.close()


def test_cross_site_post_is_rejected(client):
    resp = client.post(
        "/voorraad/handmatig",
        data={"units": "10"},
        headers={"origin": "https://kwaadaardig.example"},
    )
    assert resp.status_code == 403
    assert "geweigerd" in resp.text


# ── demo flag & empty database ───────────────────────────────────────


def test_demo_flag_serves_the_demo_database(settings):
    conn = open_db(settings.demo_db_path)
    store.add_cost_model(conn, cost_model(TODAY - timedelta(days=30), note="DEMO"))
    store.upsert_signal(
        conn,
        Signal(type="scale_up", day=TODAY, message="DEMOSIGNAAL-XYZ", details={}),
    )
    conn.commit()
    conn.close()

    demo_client = TestClient(create_app(settings, demo=True))
    body = demo_client.get("/").text
    assert "Demo-modus" in body
    assert "DEMOSIGNAAL-XYZ" in demo_client.get("/signalen").text

    # The real (empty) database never sees the demo signal.
    real_client = TestClient(create_app(settings))
    assert "DEMOSIGNAAL-XYZ" not in real_client.get("/signalen").text
    assert "Demo-modus" not in real_client.get("/").text


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_empty_database_still_renders_every_route(settings, route):
    empty_client = TestClient(create_app(settings))
    resp = empty_client.get(route)
    assert resp.status_code == 200


def test_empty_database_shows_friendly_empty_states(settings):
    empty_client = TestClient(create_app(settings))
    assert "Nog nooit een geslaagde datarun" in empty_client.get("/").text
    assert "Nog geen maandcijfers" in empty_client.get("/maand").text
    assert "nog geen kostenmodel" in empty_client.get("/instellingen").text.lower()
    assert "Geen openstaande signalen" in empty_client.get("/signalen").text


def test_unknown_route_is_a_404(client):
    assert client.get("/bestaat-niet").status_code == 404


# ── chart builders ───────────────────────────────────────────────────


def test_charts_show_dutch_empty_states_without_data():
    assert "Nog geen dagcijfers" in chart.dual_line_chart_svg([], [], [], "a", "b")
    assert "Nog geen maandcijfers" in chart.month_bars_svg([], [])
    assert "Nog geen voorraadhistorie" in chart.stock_line_svg([], [])
    assert "Nog geen betaalde orders" in chart.waterfall_svg(
        MarginBreakdown(0, 0, 0, 0, 0), "leeg"
    )


def test_dual_line_chart_renders_series_labels_and_euro_axis():
    days = [TODAY - timedelta(days=1), TODAY]
    svg = chart.dual_line_chart_svg(days, [10_000, 20_000], [5_000, 2_500], "Omzet", "Spend")
    assert svg.startswith("<svg")
    assert "Omzet" in svg and "Spend" in svg
    assert "€ 200" in svg  # euro axis label at the max gridline
    assert "<title>" in svg  # native hover tooltips


def test_waterfall_walks_from_revenue_to_margin():
    breakdown = MarginBreakdown(
        revenue_excl_cents=2748, cogs_cents=650, shipping_cents=440,
        payment_fee_cents=29, bol_commission_cents=0,
    )
    svg = chart.waterfall_svg(breakdown, "Gemiddelde order")
    assert "€ 27,48" in svg
    assert "Contributiemarge" in svg
    assert "€ 16,29" in svg  # 2748 − 650 − 440 − 29
    assert "bol-commissie" not in svg  # zero rows are skipped


def test_stock_line_draws_reorder_threshold():
    days = [TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
    svg = chart.stock_line_svg(days, [500, 480], reorder_units=390.0)
    assert "Bestelpunt" in svg
    assert "stroke-dasharray" in svg


def test_month_bars_handles_a_loss_making_month():
    svg = chart.month_bars_svg(
        ["2026-05", "2026-06"],
        [("Omzet", [100_000, 90_000]), ("Marge", [20_000, -5_000])],
    )
    assert svg.startswith("<svg")
    assert "2026-05" in svg and "2026-06" in svg
