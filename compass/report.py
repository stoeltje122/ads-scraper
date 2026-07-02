"""Weekly report for the founders: kerncijfers, Meta-vs-werkelijk honesty
panel, active signals, stock status and a plain-Dutch interpretation.

Output: Markdown (always) and a simple standalone HTML twin, written to
reports/. The Notifier interface (notify.py) can later push these to
e-mail/Slack without touching this module.

The report reads only the daily_metrics rollup and the signal/inventory
tables via compass.queries; every ratio comes from compass.metrics through
the Totals properties, so no formula lives twice.
"""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from compass import metrics, queries
from compass.models import (
    DISCLAIMER,
    NO_BOOKKEEPING,
    CostModel,
    InventoryRecord,
    ams_today,
    fmt_eur,
)
from compass.queries import Totals
from compass.signals import signal_type_label


# ── Dutch display formatting (report edge only; money via fmt_eur) ───


def _fmt_day(day: date) -> str:
    return day.strftime("%d-%m-%Y")


def _fmt_ratio(value: float | None, decimals: int = 2) -> str:
    """Fixed decimals with a Dutch comma: 2.198 → '2,20'; None → '—'."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".replace(".", ",")


def _fmt_num(value: float) -> str:
    """Trim trailing zeros, Dutch comma: 39.0 → '39', 1.3 → '1,3'."""
    return f"{value:g}".replace(".", ",")


def _fmt_int(value: int) -> str:
    """Thousands with Dutch dots: 1234567 → '1.234.567'."""
    return f"{value:,}".replace(",", ".")


def _pct(fraction: float) -> str:
    """A fraction as a Dutch percentage: 0.25 → '25,0%'."""
    return f"{fraction * 100:.1f}%".replace(".", ",")


def _pct_change(now: float | int | None, prev: float | int | None) -> str:
    """Week-over-week change; '—' when last week has nothing to compare."""
    if now is None or prev is None or prev == 0:
        return "—"
    change = (now - prev) / prev * 100
    text = f"{change:.1f}".replace(".", ",")
    if text in ("0,0", "-0,0"):
        return "0,0%"
    return f"+{text}%" if change > 0 else f"{text}%"


# ── the three plain-Dutch takeaway sentences (rule-based) ────────────


def _sentence_profitability(week: Totals) -> str:
    mer, be_roas = week.mer, week.break_even_roas
    if mer is None:
        return (
            "Er is deze week geen geld aan advertenties uitgegeven, dus de "
            "vraag of de ads zichzelf terugverdienen speelt nu niet."
        )
    if be_roas is None:
        return (
            "Er is wel advertentiegeld uitgegeven, maar de marge was deze "
            "week niet positief — controleer eerst het kostenmodel en de "
            "prijzen voordat je meer uitgeeft."
        )
    if mer >= be_roas:
        return (
            f"De advertenties verdienden zichzelf deze week terug: elke euro "
            f"spend leverde {_fmt_ratio(mer)} euro omzet excl. btw op, en "
            f"{_fmt_ratio(be_roas)} is genoeg om quitte te spelen."
        )
    return (
        f"De advertenties draaiden deze week onder break-even: elke euro "
        f"spend leverde {_fmt_ratio(mer)} euro omzet excl. btw op, terwijl "
        f"{_fmt_ratio(be_roas)} nodig is om quitte te spelen."
    )


def _sentence_trend(week: Totals, prior: Totals) -> str:
    now, prev = week.revenue_incl_cents, prior.revenue_incl_cents
    if now == 0 and prev == 0:
        return "Er was deze week geen omzet, net als vorige week."
    if prev == 0:
        return (
            f"Vorige week was er nog geen omzet; deze week kwam er "
            f"{fmt_eur(now)} binnen."
        )
    change = (now - prev) / prev
    if abs(change) < 0.02:
        return "De omzet bleef vrijwel gelijk aan vorige week."
    if change > 0:
        return f"De omzet steeg met {_pct(change)} ten opzichte van vorige week."
    return f"De omzet daalde met {_pct(-change)} ten opzichte van vorige week."


def _sentence_stock(
    inventory: InventoryRecord | None,
    days_left: float | None,
    threshold_days: float | None,
) -> str:
    if inventory is None:
        return (
            "Er is nog geen voorraadstand bekend; vul een telling in op de "
            "voorraadpagina zodat Compass kan waarschuwen voordat de "
            "voorraad op is."
        )
    if days_left is None:
        return (
            "Er is de afgelopen 30 dagen niets verkocht, dus de huidige "
            "voorraad raakt voorlopig niet op."
        )
    if threshold_days is None:
        return (
            f"Er is nog ongeveer {int(days_left)} dagen voorraad; vul het "
            f"kostenmodel in om ook een bestelpunt te krijgen."
        )
    if days_left < threshold_days:
        return (
            f"Bestel nu nieuwe voorraad: nog ongeveer {int(days_left)} dagen, "
            f"en dat is minder dan het bestelpunt van "
            f"{_fmt_num(threshold_days)} dagen."
        )
    if days_left < 1.5 * threshold_days:
        return (
            f"Het bestelpunt nadert: nog ongeveer {int(days_left)} dagen "
            f"voorraad bij een bestelpunt van {_fmt_num(threshold_days)} dagen."
        )
    return (
        f"De voorraad is ruim voldoende: nog ongeveer {int(days_left)} dagen "
        f"bij het huidige verkooptempo."
    )


# ── report builder ───────────────────────────────────────────────────


def build_weekly_markdown(conn: sqlite3.Connection, today: date | None = None) -> str:
    """The weekly report over the last 7 *complete* days (today−7 t/m
    today−1), compared with the week before. Today is always a partial
    day and never counts."""
    today = today or ams_today()
    start, end = today - timedelta(days=7), today - timedelta(days=1)
    prev_start, prev_end = today - timedelta(days=14), today - timedelta(days=8)

    lines: list[str] = []
    add = lines.append
    add(f"# Compass weekrapport — week tot {_fmt_day(today)}")
    add("")

    if queries.data_bounds(conn) is None:
        add(
            "Er staat nog geen data in Compass, dus dit weekrapport heeft "
            "nog niets te melden."
        )
        add("")
        add(
            "Draai 'compass collect' (of 'compass demo', of een CSV-import "
            "via 'compass import') en de weekcijfers verschijnen hier vanzelf."
        )
        add("")
        _footer(add)
        return "\n".join(lines)

    week = queries.window_totals(conn, start, end)
    prior = queries.window_totals(conn, prev_start, prev_end)

    add(
        f"Periode: {_fmt_day(start)} t/m {_fmt_day(end)} — de laatste 7 "
        f"volledige dagen, vergeleken met {_fmt_day(prev_start)} t/m "
        f"{_fmt_day(prev_end)}."
    )
    add("")

    # 1. Kerncijfers, this week next to last week.
    add("## Kerncijfers")
    add("")
    add("| Kerncijfer | Deze week | Vorige week | Verschil |")
    add("|---|---:|---:|---:|")
    rows = [
        ("Omzet totaal (incl. btw)", fmt_eur,
         week.revenue_incl_cents, prior.revenue_incl_cents),
        ("Omzet Shopify (incl. btw)", fmt_eur,
         week.revenue_shopify_cents, prior.revenue_shopify_cents),
        ("Omzet bol (incl. btw)", fmt_eur,
         week.revenue_bol_cents, prior.revenue_bol_cents),
        ("Orders", _fmt_int, week.orders_count, prior.orders_count),
        ("AOV (gemiddelde orderwaarde)", fmt_eur, week.aov_cents, prior.aov_cents),
        ("Ad spend", fmt_eur, week.spend_cents, prior.spend_cents),
        ("MER (omzet excl. btw / spend)", _fmt_ratio, week.mer, prior.mer),
        ("Break-even ROAS", _fmt_ratio, week.break_even_roas, prior.break_even_roas),
        ("Contributiemarge", fmt_eur, week.margin_cents, prior.margin_cents),
        ("Nieuwe klanten", _fmt_int, week.new_customers, prior.new_customers),
        ("CAC (kosten per nieuwe klant)", fmt_eur, week.cac_cents, prior.cac_cents),
    ]
    for label, fmt, now_value, prev_value in rows:
        add(
            f"| {label} | {fmt(now_value)} | {fmt(prev_value)} "
            f"| {_pct_change(now_value, prev_value)} |"
        )
    add("")

    # 2. Attribution honesty: Meta's claim next to the actual tills.
    add("## Volgens Meta vs werkelijk")
    add("")
    add(
        f"- Aankopen volgens Meta: {_fmt_int(week.meta_purchases)} — "
        f"werkelijke orders (Shopify + bol): {_fmt_int(week.orders_count)}"
    )
    add(
        f"- Aankoopwaarde volgens Meta: {fmt_eur(week.meta_purchase_value_cents)} "
        f"— werkelijke omzet (incl. btw): {fmt_eur(week.revenue_incl_cents)}"
    )
    gap = week.meta_purchase_value_cents - week.revenue_incl_cents
    if week.revenue_incl_cents > 0:
        if gap == 0:
            add(
                "- Verschil: € 0,00 — Meta en de kassa komen deze week "
                "precies overeen (toeval; attributie meet iets anders)."
            )
        else:
            direction = "minder" if gap < 0 else "meer"
            share = abs(gap) / week.revenue_incl_cents
            add(
                f"- Verschil: {fmt_eur(abs(gap))} — Meta rapporteert "
                f"{_pct(share)} {direction} dan de werkelijke omzet."
            )
    else:
        add(
            f"- Verschil: {fmt_eur(gap)} — er was deze week geen werkelijke "
            f"omzet om tegen af te zetten."
        )
    add("")
    add(
        "Meta telt alleen aankopen die het aan de eigen advertenties "
        "toeschrijft (en soms te ruim); de kassa van Shopify en bol is de "
        "werkelijkheid. Een verschil is normaal."
    )
    add("")

    # 3. Active signals with their Dutch labels and explanations.
    add("## Actieve signalen")
    add("")
    signals = queries.active_signals(conn)
    if not signals:
        add("Geen actieve signalen.")
    else:
        for sig in signals:
            add(f"- **{signal_type_label(sig.type)}** — {_fmt_day(sig.day)}: {sig.message}")
            for line in (sig.explanation or "").splitlines():
                if line.strip():
                    add(f"  > {line.strip()}")
    add("")

    # 4. Stock: level, run rate, reorder point, expected sell-out day.
    add("## Voorraad")
    add("")
    inventory = queries.latest_inventory(conn)
    cost_model = queries.current_cost_model(conn, today)
    rate = metrics.weighted_daily_sales(
        queries.units_sold_by_day(conn, today - timedelta(days=30), end)
    )
    days_left = (
        metrics.days_of_stock(inventory.units, rate) if inventory is not None else None
    )
    threshold_days = _threshold_days(cost_model)
    if inventory is None:
        add(
            "Nog geen voorraadstand bekend — vul een handmatige telling in "
            "op de voorraadpagina van het dashboard."
        )
    else:
        add(
            f"- Voorraad: {_fmt_int(inventory.units)} stuks "
            f"(bron: {inventory.source}, peildatum {_fmt_day(inventory.day)})"
        )
        add(f"- Verkoopsnelheid (gewogen, 30 dagen): {_fmt_ratio(rate, 1)} per dag")
        if days_left is None:
            add("- Dagen voorraad: — (geen verkopen in de afgelopen 30 dagen)")
        else:
            add(f"- Dagen voorraad: ongeveer {int(days_left)}")
        if cost_model is None:
            add("- Bestelpunt: — (nog geen kostenmodel ingevuld)")
        else:
            reorder_units = metrics.reorder_point_units(
                rate, cost_model.lead_time_days, cost_model.safety_factor
            )
            add(
                f"- Bestelpunt: {_fmt_int(int(reorder_units + 0.5))} stuks "
                f"({_fmt_num(threshold_days)} dagen voorraad)"
            )
        sellout = metrics.sellout_day(today, days_left)
        if sellout is None:
            add("- Verwachte uitverkoopdatum: voorlopig niet in zicht")
        else:
            add(f"- Verwachte uitverkoopdatum: rond {_fmt_day(sellout)}")
    add("")

    # 5. Exactly three plain-Dutch takeaways, chosen rule-based.
    add("## Wat betekent dit")
    add("")
    add(f"- {_sentence_profitability(week)}")
    add(f"- {_sentence_trend(week, prior)}")
    add(f"- {_sentence_stock(inventory, days_left, threshold_days)}")
    add("")
    _footer(add)
    return "\n".join(lines)


def _threshold_days(cost_model: CostModel | None) -> float | None:
    if cost_model is None:
        return None
    return cost_model.lead_time_days * cost_model.safety_factor


def _footer(add) -> None:
    add("---")
    add("")
    add(f"*{DISCLAIMER}*")
    add(f"*{NO_BOOKKEEPING}*")


def write_weekly(
    conn: sqlite3.Connection, reports_dir: Path, today: date | None = None
) -> tuple[Path, Path]:
    today = today or ams_today()
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = build_weekly_markdown(conn, today)
    md_path = reports_dir / f"compass-week-{today.isoformat()}.md"
    md_path.write_text(md, encoding="utf-8")
    html_path = reports_dir / f"compass-week-{today.isoformat()}.html"
    html_path.write_text(_markdown_to_html(md), encoding="utf-8")
    return md_path, html_path


# ── tiny md→html twin (same hand-rolled converter as adscout.report) ─


def _markdown_to_html(md: str) -> str:
    """Tiny, dependency-free markdown-to-HTML for our own report structure."""
    out: list[str] = [
        "<!doctype html><html lang='nl'><head><meta charset='utf-8'>",
        "<title>Compass weekrapport</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;"
        "padding:0 1rem;color:#1a1a2e}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ddd;padding:6px 10px;text-align:left}"
        "th{background:#f4f4f8}blockquote{color:#555;border-left:3px solid #ccc;"
        "margin:4px 0 4px 12px;padding-left:10px}</style></head><body>",
    ]
    in_table = False
    in_list = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # separator row
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        if in_list and not stripped.startswith(">"):
            out.append("</ul>")
            in_list = False
        if stripped.startswith("### "):
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif stripped == "---":
            out.append("<hr>")
        elif stripped:
            out.append(f"<p>{_inline(stripped)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text
