"""Compass dashboard: FastAPI app factory.

Server-rendered Jinja2 pages, POST-redirect-GET for mutations, one fresh
sqlite connection per request. No CDN assets, no JS: one hand-written
CSS file and inline server-rendered SVG charts.

The app is strictly read-only towards the outside world; the only writes
go to Compass' own database (manual stock counts, cost-model versions,
signal thresholds, mark-signal-seen).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from compass import metrics, queries, signals, store
from compass.config import Settings, load_settings
from compass.db import open_db
from compass.models import (
    AMSTERDAM,
    DISCLAIMER,
    NO_BOOKKEEPING,
    CostModel,
    PaymentFee,
    ams_today,
    fmt_eur,
    parse_eur_to_cents,
)
from compass.web import chart

WEB_DIR = Path(__file__).parent

SPEND_WINDOWS = (7, 30, 90)
STALE_AFTER_HOURS = 36

_MONTHS_NL = (
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
)

ATTRIBUTION_EXPLAINER = (
    "Meta telt aankopen via de pixel en zijn eigen attributieregels "
    "(bijvoorbeeld tot 7 dagen na een klik); Shopify en bol tellen "
    "daadwerkelijke bestellingen. Die twee meten per definitie iets "
    "anders — een verschil is normaal en geen fout."
)


# ── Dutch display formatting (the only place cents become euros) ─────


def _nlnum(value) -> str:
    """1234567 → '1.234.567' (Dutch thousands separator); None → '—'."""
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", ".")


def _fmt_pct(value: float | None, decimals: int = 1) -> str:
    """0.5928 → '59,3%'; None → '—'."""
    if value is None:
        return "—"
    return f"{value * 100:.{decimals}f}".replace(".", ",") + "%"


def _fmt_ratio(value: float | None, decimals: int = 2) -> str:
    """2.748 → '2,75'; None → '—'."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".replace(".", ",")


def _fmt_num(value: float | None) -> str:
    """Trim trailing zeros, Dutch comma: 2.0 → '2', 1.3 → '1,3'."""
    if value is None:
        return "—"
    return f"{value:g}".replace(".", ",")


def _fmt_day(value) -> str:
    """date or ISO string → 'dd-mm-jjjj'; None/empty → '—'."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%d-%m-%Y")


def _month_label(month: str) -> str:
    """'2026-06' → 'juni 2026'."""
    try:
        year, m = month.split("-")
        return f"{_MONTHS_NL[int(m) - 1]} {int(year)}"
    except (ValueError, IndexError):
        return month


def _pretty_json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


def _eur_input(cents: int | None) -> str:
    """Cents → form-input euros ('6,50') — no € sign, Dutch comma."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros},{rest:02d}"


def _pct_input(fraction: float | None) -> str:
    """Fraction → form-input percentage: 0.015 → '1,5'."""
    if fraction is None:
        return ""
    return f"{fraction * 100:g}".replace(".", ",")


# ── form parsing (Dutch comma tolerant, soft failure) ────────────────


def _parse_float(text, default: float = 0.0) -> float:
    text = str(text or "").strip().replace(",", ".")
    if not text:
        return default
    return float(text)


def _parse_pct(text) -> float:
    """A percentage as humans write it ('1,5' = 1,5%) → fraction 0.015."""
    return _parse_float(text) / 100.0


def _parse_units(text) -> int:
    """A stock count; tolerate thousands dots ('1.500') and spaces."""
    cleaned = str(text or "").strip().replace(".", "").replace(" ", "")
    return int(cleaned)


def _hours_since(iso: str | None) -> float | None:
    """Age of an ISO timestamp in hours; naive stamps count as Amsterdam
    wall-clock time (the Compass day policy). None when unparseable."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=AMSTERDAM)
    return (datetime.now(AMSTERDAM) - moment).total_seconds() / 3600.0


# ── page building blocks ─────────────────────────────────────────────


def _mer_verdict(totals: queries.Totals, scale_factor: float) -> dict:
    """The MER-vs-break-even verdict for the header block: green when the
    MER clears the scale-up factor, amber between, red below break-even."""
    mer_value, be = totals.mer, totals.break_even_roas
    if mer_value is None or be is None:
        return {
            "kind": "none",
            "label": "Nog geen oordeel",
            "text": (
                "Nog geen MER te berekenen — daarvoor zijn zowel ad spend "
                "als omzet in de laatste 7 dagen nodig."
            ),
        }
    numbers = (
        f"MER {_fmt_ratio(mer_value)} (omzet excl. btw / spend) vs "
        f"break-even ROAS {_fmt_ratio(be)}"
    )
    if mer_value >= scale_factor * be:
        return {
            "kind": "good",
            "label": "Gezond",
            "text": f"{numbers} — minstens {_fmt_num(scale_factor)}× boven break-even.",
        }
    if mer_value >= be:
        return {
            "kind": "warn",
            "label": "Rond break-even",
            "text": f"{numbers} — winstgevend, maar de marge is dun.",
        }
    return {
        "kind": "bad",
        "label": "Onder break-even",
        "text": f"{numbers} — advertenties kosten nu meer dan ze opleveren.",
    }


def _meta_vs_actual(totals: queries.Totals) -> dict:
    """Meta's attribution claim next to the real Shopify+bol numbers."""
    actual = totals.revenue_incl_cents
    claimed = totals.meta_purchase_value_cents
    diff = claimed - actual
    return {
        "meta_purchases": totals.meta_purchases,
        "meta_value_cents": claimed,
        "orders_count": totals.orders_count,
        "revenue_incl_cents": actual,
        "diff_cents": diff,
        "diff_pct": (diff / actual) if actual > 0 else None,
    }


def _daily_series(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[list[date], list[int], list[int]]:
    """(days, revenue excl. btw, spend) with zero-filled gaps for charts."""
    rows = {row["day"]: row for row in queries.day_rows(conn, start, end)}
    days: list[date] = []
    revenue: list[int] = []
    spend: list[int] = []
    day = start
    while day <= end:
        days.append(day)
        row = rows.get(day.isoformat())
        revenue.append(row["revenue_excl_cents"] if row else 0)
        spend.append(row["spend_cents"] if row else 0)
        day += timedelta(days=1)
    return days, revenue, spend


def _campaign_spend_series(
    conn: sqlite3.Connection, start: date, end: date, totals_rows: list[sqlite3.Row]
) -> tuple[list[date], list[tuple[str, list[int]]]]:
    """Per-day spend series for the top campaigns; the rest folds into
    'Overig' so the palette is never cycled."""
    days: list[date] = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    index = {d.isoformat(): i for i, d in enumerate(days)}

    top_ids = [row["campaign_id"] for row in totals_rows[: len(chart.SERIES_COLORS)]]
    names = {row["campaign_id"]: row["campaign_name"] for row in totals_rows}
    series: dict[str, list[int]] = {cid: [0] * len(days) for cid in top_ids}
    other = [0] * len(days)
    has_other = False
    for row in queries.campaign_day_rows(conn, start, end):
        i = index[row["day"]]
        if row["campaign_id"] in series:
            series[row["campaign_id"]][i] += row["spend_cents"]
        else:
            other[i] += row["spend_cents"]
            has_other = True
    out = [(names[cid] or cid, series[cid]) for cid in top_ids]
    if has_other:
        out.append(("Overig", other))
    return days, out


def _freshness(conn: sqlite3.Connection) -> dict:
    """Freshness banner data: warn when the last successful run is stale."""
    run = queries.last_successful_run(conn)
    if run is None:
        return {
            "banner": (
                "Nog nooit een geslaagde datarun gedaan — draai `compass collect` "
                "(of probeer eerst `compass demo`)."
            ),
            "line": None,
        }
    stamp = run["finished_at"] or run["started_at"]
    age = _hours_since(stamp)
    if age is not None and age > STALE_AFTER_HOURS:
        return {
            "banner": (
                f"De laatste geslaagde run was {int(age)} uur geleden "
                f"({_fmt_day(str(stamp))}) — de cijfers hieronder lopen achter. "
                "Draai `compass collect`."
            ),
            "line": None,
        }
    return {"banner": None, "line": f"Laatste geslaagde run: {stamp}"}


def _table_bounds(
    conn: sqlite3.Connection, table: str, day_col: str, where: str = "", params: tuple = ()
) -> tuple[str, str] | None:
    row = conn.execute(
        f"SELECT MIN({day_col}) AS lo, MAX({day_col}) AS hi FROM {table} {where}", params
    ).fetchone()
    if row["lo"] is None:
        return None
    return row["lo"], row["hi"]


# ── App factory ──────────────────────────────────────────────────────


def create_app(settings: Settings | None = None, demo: bool = False) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Compass", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """Reject cross-site POSTs (CSRF guard for a localhost tool).

        Browsers attach an Origin header to cross-origin form posts; a
        mismatch with the Host we're serving on means some other website is
        firing requests at the dashboard. Same-origin posts and non-browser
        clients (no Origin header) pass through.
        """
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return Response("Cross-site POST geweigerd.", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals.update(
        fmt_eur=fmt_eur,
        nlnum=_nlnum,
        fmt_pct=_fmt_pct,
        fmt_ratio=_fmt_ratio,
        fmt_num=_fmt_num,
        fmt_day=_fmt_day,
        month_label=_month_label,
        pretty_json=_pretty_json,
        eur_input=_eur_input,
        pct_input=_pct_input,
        signal_type_label=signals.signal_type_label,
        DISCLAIMER=DISCLAIMER,
        NO_BOOKKEEPING=NO_BOOKKEEPING,
        demo=demo,
    )
    templates.env.filters["day"] = lambda v: (v or "")[:10]

    db_path = settings.demo_db_path if demo else settings.db_path

    async def get_conn() -> AsyncIterator[sqlite3.Connection]:
        conn = open_db(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, conn: sqlite3.Connection, name: str, context: dict):
        # The nav badge on "Signalen" needs the open-signal count on every page.
        context.setdefault("active_count", len(queries.active_signals(conn)))
        return templates.TemplateResponse(request, name, context)

    # ── Vandaag ──────────────────────────────────────────────────────

    @app.get("/")
    async def vandaag(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        today = ams_today()
        today_totals = queries.window_totals(conn, today, today)
        week = queries.window_totals(conn, today - timedelta(days=6), today)
        thresholds = signals.get_thresholds(conn)
        # Chart ends yesterday: today is a partial day and would always
        # plot as a scary nosedive to €0.
        days, revenue, spend = _daily_series(
            conn, today - timedelta(days=30), today - timedelta(days=1)
        )
        open_signals = queries.active_signals(conn)
        return render(
            request,
            conn,
            "vandaag.html",
            {
                "nav": "vandaag",
                "today": today,
                "vandaag": today_totals,
                "week": week,
                "verdict": _mer_verdict(week, thresholds["mer_scale_factor"]),
                "meta_gap": _meta_vs_actual(week),
                "chart_svg": chart.dual_line_chart_svg(
                    days, revenue, spend, "Omzet excl. btw", "Ad spend"
                ),
                "signals_open": open_signals,
                "active_count": len(open_signals),
                "freshness": _freshness(conn),
                "attribution_explainer": ATTRIBUTION_EXPLAINER,
            },
        )

    # ── Maand ────────────────────────────────────────────────────────

    @app.get("/maand")
    async def maand(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        today = ams_today()
        months = queries.month_rows(conn, today, months=12)
        chart_svg = chart.month_bars_svg(
            [m.month for m in months],
            [
                ("Omzet incl. btw", [m.totals.revenue_incl_cents for m in months]),
                ("Ad spend", [m.totals.spend_cents for m in months]),
                ("Marge", [m.totals.margin_cents for m in months]),
            ],
        )
        return render(
            request,
            conn,
            "maand.html",
            {
                "nav": "maand",
                "months": months,
                "current_month": today.strftime("%Y-%m"),
                "chart_svg": chart_svg,
            },
        )

    # ── Advertenties ─────────────────────────────────────────────────

    @app.get("/advertenties")
    async def advertenties(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        today = ams_today()
        try:
            dagen = int(request.query_params.get("dagen", "30"))
        except ValueError:
            dagen = 30
        if dagen not in SPEND_WINDOWS:
            dagen = 30
        # Complete days only: today is partial and would nosedive the chart
        # and understate today's campaign rows.
        start, end = today - timedelta(days=dagen), today - timedelta(days=1)

        totals_rows = queries.campaign_totals(conn, start, end)
        campaigns = [
            {
                "campaign_id": row["campaign_id"],
                "campaign_name": row["campaign_name"],
                "spend_cents": row["spend_cents"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                # CTR is a plain count ratio (no money) — composed here, not a metric.
                "ctr": (row["clicks"] / row["impressions"]) if row["impressions"] else None,
                "meta_purchases": row["meta_purchases"],
                "meta_purchase_value_cents": row["meta_purchase_value_cents"],
                "meta_roas": metrics.meta_roas(
                    row["meta_purchase_value_cents"], row["spend_cents"]
                ),
            }
            for row in totals_rows
        ]
        window = queries.window_totals(conn, start, end)
        days, series = _campaign_spend_series(conn, start, end, totals_rows)
        return render(
            request,
            conn,
            "advertenties.html",
            {
                "nav": "advertenties",
                "dagen": dagen,
                "windows": SPEND_WINDOWS,
                "start": start,
                "end": end,
                "campaigns": campaigns,
                "window": window,
                "meta_gap": _meta_vs_actual(window),
                "day_rows": queries.campaign_day_rows(conn, start, end),
                "chart_svg": chart.campaign_lines_svg(days, series),
                "attribution_explainer": ATTRIBUTION_EXPLAINER,
            },
        )

    # ── Economie ─────────────────────────────────────────────────────

    @app.get("/economie")
    async def economie(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        today = ams_today()
        # Unit economics over the last 30 *complete* days: today is a
        # partial day and would water down every average.
        start, end = today - timedelta(days=30), today - timedelta(days=1)
        window = queries.window_totals(conn, start, end)
        channels = []
        for channel, label in (("shopify", "Shopify (eigen shop)"), ("bol", "bol")):
            econ = queries.channel_totals(conn, start, end, channel)
            channels.append(
                {
                    "label": label,
                    "econ": econ,
                    "waterfall_svg": chart.waterfall_svg(
                        econ.avg, f"Gemiddelde order via {label}"
                    )
                    if econ.avg is not None
                    else None,
                }
            )
        be = window.break_even_roas
        # Display edge: the break-even ratio expressed as euros-per-euro.
        be_eur = fmt_eur(int(be * 100 + 0.5)) if be is not None else None
        return render(
            request,
            conn,
            "economie.html",
            {
                "nav": "economie",
                "start": start,
                "end": end,
                "window": window,
                "channels": channels,
                "break_even_eur": be_eur,
            },
        )

    # ── Voorraad ─────────────────────────────────────────────────────

    @app.get("/voorraad")
    async def voorraad(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        today = ams_today()
        inventory = queries.latest_inventory(conn)
        sales_rate = metrics.weighted_daily_sales(
            queries.units_sold_by_day(
                conn, today - timedelta(days=30), today - timedelta(days=1)
            )
        )
        cost_model = queries.current_cost_model(conn, today)
        days_left = (
            metrics.days_of_stock(inventory.units, sales_rate)
            if inventory is not None
            else None
        )
        reorder_units = threshold_days = None
        if cost_model is not None:
            reorder_units = metrics.reorder_point_units(
                sales_rate, cost_model.lead_time_days, cost_model.safety_factor
            )
            threshold_days = cost_model.lead_time_days * cost_model.safety_factor
        sellout = metrics.sellout_day(today, days_left)

        if inventory is None:
            verdict = {
                "kind": "none",
                "label": "Geen voorraadstand",
                "text": (
                    "Nog geen voorraadstand bekend — vul hieronder een "
                    "handmatige telling in of draai `compass collect`."
                ),
            }
        elif days_left is None:
            verdict = {
                "kind": "good",
                "label": "OK",
                "text": "Er is voorraad en er lopen (nog) geen verkopen uit de teller.",
            }
        elif threshold_days is not None and days_left < threshold_days:
            verdict = {
                "kind": "bad",
                "label": "Bestel nu",
                "text": (
                    f"Nog ±{int(days_left)} dagen voorraad — dat is minder dan het "
                    f"bestelpunt van {_fmt_num(threshold_days)} dagen "
                    "(levertijd × veiligheidsfactor)."
                ),
            }
        else:
            verdict = {
                "kind": "good",
                "label": "OK",
                "text": f"Nog ±{int(days_left)} dagen voorraad bij de huidige verkoopsnelheid.",
            }

        series_days, series_units = chart.inventory_rows_to_series(
            queries.inventory_series(conn, today - timedelta(days=89), today)
        )
        return render(
            request,
            conn,
            "voorraad.html",
            {
                "nav": "voorraad",
                "inventory": inventory,
                "sales_rate": sales_rate,
                "days_left": days_left,
                "reorder_units": reorder_units,
                "threshold_days": threshold_days,
                "sellout": sellout,
                "verdict": verdict,
                "cost_model": cost_model,
                "chart_svg": chart.stock_line_svg(series_days, series_units, reorder_units),
                "fout": request.query_params.get("fout"),
            },
        )

    @app.post("/voorraad/handmatig")
    async def voorraad_handmatig(
        units: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        try:
            value = _parse_units(units)
        except ValueError:
            return RedirectResponse("/voorraad?fout=telling", status_code=303)
        store.set_manual_inventory(conn, ams_today(), max(value, 0))
        return RedirectResponse("/voorraad", status_code=303)

    # ── Signalen ─────────────────────────────────────────────────────

    @app.get("/signalen")
    async def signalen(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        open_signals = queries.active_signals(conn)
        return render(
            request,
            conn,
            "signalen.html",
            {
                "nav": "signalen",
                "signals_open": open_signals,
                "history": queries.all_signals(conn),
                "active_count": len(open_signals),
            },
        )

    @app.post("/signalen/{signal_id}/gezien")
    async def signal_gezien(
        signal_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ):
        if queries.signal_by_id(conn, signal_id) is None:
            raise HTTPException(status_code=404, detail="Signaal niet gevonden")
        store.mark_signal_seen(conn, signal_id)
        return RedirectResponse("/signalen", status_code=303)

    # ── Instellingen ─────────────────────────────────────────────────

    @app.get("/instellingen")
    async def instellingen(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        today = ams_today()
        # Data-based break-even for the current model: last 30 complete days.
        month_window = queries.window_totals(
            conn, today - timedelta(days=30), today - timedelta(days=1)
        )
        sources = [
            {
                "label": "Shopify",
                "configured": settings.has_shopify(),
                "bounds": _table_bounds(
                    conn, "orders", "order_day", "WHERE channel = ?", ("shopify",)
                ),
            },
            {
                "label": "Meta",
                "configured": settings.has_meta(),
                "bounds": _table_bounds(conn, "ad_spend_daily", "day"),
            },
            {
                "label": "bol",
                "configured": settings.has_bol(),
                "bounds": _table_bounds(
                    conn, "orders", "order_day", "WHERE channel = ?", ("bol",)
                ),
            },
        ]
        return render(
            request,
            conn,
            "instellingen.html",
            {
                "nav": "instellingen",
                "today": today,
                "cost_model": queries.current_cost_model(conn, today),
                "history": queries.cost_model_history(conn),
                "break_even_30d": month_window.break_even_roas,
                "thresholds": signals.get_thresholds(conn),
                "sources": sources,
                "runs": queries.last_runs(conn, 5),
                "fout": request.query_params.get("fout"),
            },
        )

    @app.post("/instellingen/kostenmodel")
    async def instellingen_kostenmodel(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        form = await request.form()
        today = ams_today()
        try:
            payment_fees: dict[str, PaymentFee] = {}
            for i in range(1, 5):
                method = str(form.get(f"pf_method_{i}") or "").strip().lower()
                if not method:
                    continue
                payment_fees[method] = PaymentFee(
                    pct=_parse_pct(form.get(f"pf_pct_{i}")),
                    fixed_cents=parse_eur_to_cents(str(form.get(f"pf_fixed_{i}") or "")) or 0,
                )
            valid_raw = str(form.get("valid_from") or "").strip()
            cm = CostModel(
                valid_from=date.fromisoformat(valid_raw) if valid_raw else today,
                cogs_per_unit_cents=parse_eur_to_cents(str(form.get("cogs_eur") or "")) or 0,
                shipping_per_order_cents=parse_eur_to_cents(str(form.get("shipping_eur") or ""))
                or 0,
                fee_pct=_parse_pct(form.get("fee_pct")),
                fee_fixed_cents=parse_eur_to_cents(str(form.get("fee_fixed_eur") or "")) or 0,
                payment_fees=payment_fees,
                bol_commission_pct=_parse_pct(form.get("bol_commission_pct")),
                vat_rate=_parse_pct(form.get("vat_pct")),
                fixed_month_cents=parse_eur_to_cents(str(form.get("fixed_month_eur") or ""))
                or 0,
                lead_time_days=int(str(form.get("lead_time_days") or "30").strip() or 30),
                safety_factor=_parse_float(form.get("safety_factor"), default=1.3),
                note=str(form.get("note") or "").strip() or None,
            )
        except (ValueError, ArithmeticError):
            return RedirectResponse("/instellingen?fout=kostenmodel", status_code=303)
        store.add_cost_model(conn, cm)
        # A new model version changes historic margins: rebuild the rollup.
        bounds = queries.data_bounds(conn)
        if bounds is not None:
            store.rebuild_daily_metrics(conn, bounds[0], bounds[1])
        return RedirectResponse("/instellingen", status_code=303)

    @app.post("/instellingen/drempels")
    async def instellingen_drempels(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        form = await request.form()
        for key in signals.THRESHOLD_DEFAULTS:
            raw = str(form.get(key) or "").strip()
            if not raw:
                continue
            try:
                _parse_float(raw)
            except ValueError:
                continue  # soft: a typo never breaks the other thresholds
            store.set_setting(conn, f"signal.{key}", raw)
        return RedirectResponse("/instellingen", status_code=303)

    return app
