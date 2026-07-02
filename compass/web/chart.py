"""Server-rendered inline SVG charts for the Compass dashboard.

No JS chart libraries: the dashboard must work offline forever. Hover
detail comes from native SVG <title> tooltips (one generous hit target
per day/bar, never a tiny dot). Money enters as integer cents and is
formatted at this display edge via models.fmt_eur.

Colors mirror the light dashboard theme; the series palette was checked
with a palette validator (lightness band, chroma floor, CVD separation,
>= 3:1 contrast on the chart surface). Series identity never relies on
color alone: every multi-series chart draws a text legend.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Sequence
from xml.sax.saxutils import escape

from compass.metrics import MarginBreakdown
from compass.models import fmt_eur

# Chart chrome (light theme, aligned with compass.css tokens).
_SURFACE = "#fcfcfb"
_GRID = "#e1e0d9"
_BASELINE = "#c3c2b7"
_MUTED_INK = "#898781"
_INK = "#52514e"

# Validated categorical series palette (fixed order, never cycled).
SERIES_COLORS = ("#0b8064", "#b45309", "#2a78d6", "#6d4fa1")
_OTHER = "#6f6d66"      # the deliberately recessive "Overig" series
_GOOD = "#2f7d3b"
_BAD = "#b3312f"


def _nl_int(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _svg_open(width: int, height: int, aria_label: str) -> str:
    return (
        f'<svg class="compass-chart" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" preserveAspectRatio="xMinYMid meet" '
        f'aria-label="{escape(aria_label)}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_SURFACE}"/>'
    )


def _empty_svg(message: str, width: int = 680, height: int = 160) -> str:
    """A friendly placeholder instead of an empty axis skeleton."""
    return (
        _svg_open(width, height, message)
        + f'<text x="{width / 2:.1f}" y="{height / 2 + 4:.1f}" text-anchor="middle" '
        f'font-size="13" fill="{_MUTED_INK}">{escape(message)}</text></svg>'
    )


def _legend(parts: list[str], entries: list[tuple[str, str]], x: float, y: float) -> None:
    """Swatch + label per series; text wears ink, the swatch carries color."""
    for label, color in entries:
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 8:.1f}" width="10" height="10" rx="2" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{x + 15:.1f}" y="{y + 1:.1f}" font-size="11" '
            f'fill="{_INK}">{escape(label)}</text>'
        )
        x += 15 + 7 * len(label) + 22  # rough advance; labels are short Dutch words


def _gridlines(
    parts: list[str],
    *,
    max_v: int,
    pad_left: float,
    pad_top: float,
    inner_w: float,
    inner_h: float,
    fmt,
) -> None:
    """Hairlines at 0 / half / max with muted value labels (adscout style)."""
    for frac in (0.0, 0.5, 1.0):
        y = pad_top + inner_h * (1 - frac)
        color = _BASELINE if frac == 0.0 else _GRID
        parts.append(
            f'<line x1="{pad_left:.1f}" y1="{y:.1f}" x2="{pad_left + inner_w:.1f}" '
            f'y2="{y:.1f}" stroke="{color}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(fmt(round(max_v * frac)))}</text>'
        )


def _x_edge_labels(
    parts: list[str], labels: Sequence[str], pad_left: float, width: int,
    pad_right: float, y: float,
) -> None:
    """Only first and last x-label — selective, never every tick."""
    if not labels:
        return
    parts.append(
        f'<text x="{pad_left:.1f}" y="{y:.1f}" font-size="10" '
        f'fill="{_MUTED_INK}">{escape(labels[0])}</text>'
    )
    if len(labels) > 1:
        parts.append(
            f'<text x="{width - pad_right:.1f}" y="{y:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(labels[-1])}</text>'
        )


# ── line charts ──────────────────────────────────────────────────────


def _line_chart(
    days: Sequence[date],
    series: list[tuple[str, Sequence[int], str]],
    *,
    aria_label: str,
    value_fmt,
    width: int,
    height: int,
    threshold: tuple[float, str] | None = None,
    empty_message: str,
) -> str:
    """Shared line-chart body: series = [(label, values, color)].

    One transparent full-height hover column per day carries a combined
    native tooltip — a generous hit target instead of 3px dots.
    """
    if not days or not series:
        return _empty_svg(empty_message, width=width)

    pad_left, pad_right, pad_top, pad_bottom = 62.0, 12.0, 30.0, 26.0
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom

    all_values = [v for _, values, _ in series for v in values]
    max_v = max([*all_values, int(threshold[0]) if threshold else 0]) or 1
    n = len(days)

    def x_at(i: int) -> float:
        if n == 1:
            return pad_left + inner_w / 2
        return pad_left + inner_w * i / (n - 1)

    def y_at(v: float) -> float:
        return pad_top + inner_h * (1 - min(v, max_v) / max_v)

    parts = [_svg_open(width, height, aria_label)]
    _gridlines(
        parts, max_v=max_v, pad_left=pad_left, pad_top=pad_top,
        inner_w=inner_w, inner_h=inner_h, fmt=value_fmt,
    )

    if threshold is not None:
        t_value, t_label = threshold
        ty = y_at(t_value)
        parts.append(
            f'<line x1="{pad_left:.1f}" y1="{ty:.1f}" x2="{pad_left + inner_w:.1f}" '
            f'y2="{ty:.1f}" stroke="{_BAD}" stroke-width="1" stroke-dasharray="5 4"/>'
        )
        parts.append(
            f'<text x="{pad_left + inner_w:.1f}" y="{ty - 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_BAD}">{escape(t_label)}</text>'
        )

    for label, values, color in series:
        points = " ".join(
            f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values)
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # Hover columns with a combined per-day tooltip.
    col_w = inner_w / max(n, 1)
    for i, day in enumerate(days):
        lines = [
            f"{label}: {value_fmt(values[i])}" for label, values, _ in series
        ]
        title = f"<title>{escape(day.isoformat())} · {escape(' · '.join(lines))}</title>"
        parts.append(
            f'<rect x="{x_at(i) - col_w / 2:.1f}" y="{pad_top:.1f}" '
            f'width="{col_w:.1f}" height="{inner_h:.1f}" fill="transparent">{title}</rect>'
        )

    _legend(parts, [(label, color) for label, _, color in series], pad_left, 16.0)
    _x_edge_labels(
        parts, [d.isoformat() for d in days], pad_left, width, pad_right,
        height - pad_bottom + 16,
    )
    parts.append("</svg>")
    return "".join(parts)


def dual_line_chart_svg(
    days: Sequence[date],
    series_a: Sequence[int],
    series_b: Sequence[int],
    label_a: str,
    label_b: str,
    width: int = 680,
    height: int = 220,
) -> str:
    """Two euro series (cents in) as lines over days — omzet vs spend."""
    return _line_chart(
        days,
        [(label_a, series_a, SERIES_COLORS[0]), (label_b, series_b, SERIES_COLORS[1])],
        aria_label=f"{label_a} en {label_b} per dag",
        value_fmt=lambda cents: fmt_eur(int(cents), decimals=0),
        width=width,
        height=height,
        empty_message="Nog geen dagcijfers — draai `compass collect` of `compass demo`.",
    )


def campaign_lines_svg(
    days: Sequence[date],
    series: list[tuple[str, Sequence[int]]],
    width: int = 680,
    height: int = 240,
) -> str:
    """Spend per day per campaign (cents in). Callers pass at most the top
    four campaigns plus an 'Overig' rest bucket — fixed palette, never
    cycled; the rest bucket is deliberately grey."""
    if not days or not series:
        return _empty_svg(
            "Nog geen campagne-data — draai `compass collect` of `compass demo`.",
            width=width,
        )
    colored: list[tuple[str, Sequence[int], str]] = []
    for i, (label, values) in enumerate(series[: len(SERIES_COLORS) + 1]):
        color = _OTHER if i >= len(SERIES_COLORS) else SERIES_COLORS[i]
        colored.append((label, values, color))
    return _line_chart(
        days,
        colored,
        aria_label="Ad spend per dag per campagne",
        value_fmt=lambda cents: fmt_eur(int(cents), decimals=0),
        width=width,
        height=height,
        empty_message="Nog geen campagne-data.",
    )


def stock_line_svg(
    days: Sequence[date],
    units: Sequence[int],
    reorder_units: float | None = None,
    width: int = 680,
    height: int = 220,
) -> str:
    """Stock on hand over time, with the reorder point as a dashed line."""
    if not days:
        return _empty_svg(
            "Nog geen voorraadhistorie — die groeit met elke run of handmatige telling.",
            width=width,
        )
    threshold = None
    if reorder_units is not None and reorder_units > 0:
        threshold = (float(reorder_units), f"Bestelpunt: {_nl_int(round(reorder_units))} stuks")
    return _line_chart(
        days,
        [("Voorraad (stuks)", units, SERIES_COLORS[0])],
        aria_label="Voorraadverloop in stuks per dag",
        value_fmt=lambda v: _nl_int(int(v)),
        width=width,
        height=height,
        threshold=threshold,
        empty_message="Nog geen voorraadhistorie.",
    )


def inventory_rows_to_series(
    rows: list[sqlite3.Row],
) -> tuple[list[date], list[int]]:
    """(days, units) from queries.inventory_series rows — snapshots only,
    no gap filling: a missing day is 'not counted', not 'zero stock'."""
    days = [date.fromisoformat(row["day"]) for row in rows]
    return days, [int(row["units"]) for row in rows]


# ── month bars ───────────────────────────────────────────────────────


def month_bars_svg(
    months: Sequence[str],
    series: list[tuple[str, Sequence[int]]],
    width: int = 680,
    height: int = 240,
) -> str:
    """Grouped bars per month (cents in): omzet vs spend vs marge.
    Negative values (a loss-making month) hang below the zero baseline."""
    if not months or not series:
        return _empty_svg("Nog geen maandcijfers.", width=width)

    pad_left, pad_right, pad_top, pad_bottom = 62.0, 12.0, 30.0, 26.0
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom

    all_values = [v for _, values in series for v in values]
    max_v = max([*all_values, 0]) or 1
    min_v = min([*all_values, 0])
    span = (max_v - min_v) or 1

    def y_at(v: float) -> float:
        return pad_top + inner_h * (max_v - v) / span

    zero_y = y_at(0)
    n_months = len(months)
    n_series = len(series)
    group_w = inner_w / n_months
    bar_w = max(2.0, min(22.0, (group_w - 8) / n_series - 2))

    parts = [_svg_open(width, height, "Maandcijfers: " + ", ".join(l for l, _ in series))]

    # Gridlines at min / 0 / max, zero as the visual baseline.
    levels = {0: _BASELINE, max_v: _GRID}
    if min_v < 0:
        levels[min_v] = _GRID
    for value, color in levels.items():
        y = y_at(value)
        parts.append(
            f'<line x1="{pad_left:.1f}" y1="{y:.1f}" x2="{pad_left + inner_w:.1f}" '
            f'y2="{y:.1f}" stroke="{color}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(fmt_eur(value, decimals=0))}</text>'
        )

    colors = [SERIES_COLORS[i % len(SERIES_COLORS)] for i in range(n_series)]
    for m_index, month in enumerate(months):
        group_x = pad_left + m_index * group_w + (group_w - n_series * (bar_w + 2)) / 2
        for s_index, (label, values) in enumerate(series):
            value = values[m_index]
            x = group_x + s_index * (bar_w + 2)
            top = min(y_at(value), zero_y)
            h = abs(y_at(value) - zero_y)
            title = f"<title>{escape(month)} · {escape(label)}: {escape(fmt_eur(value))}</title>"
            if h < 1:  # zero-ish: keep the month inspectable
                parts.append(
                    f'<rect x="{x:.1f}" y="{zero_y - 2:.1f}" width="{bar_w:.1f}" '
                    f'height="2" fill="transparent">{title}</rect>'
                )
                continue
            fill = _BAD if value < 0 else colors[s_index]
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'rx="2" fill="{fill}">{title}</rect>'
            )

    _legend(parts, list(zip((l for l, _ in series), colors)), pad_left, 16.0)
    _x_edge_labels(parts, list(months), pad_left, width, pad_right, height - pad_bottom + 16)
    parts.append("</svg>")
    return "".join(parts)


# ── waterfall (unit economics) ───────────────────────────────────────


def waterfall_svg(breakdown: MarginBreakdown, title: str, width: int = 680) -> str:
    """Horizontal waterfall from omzet excl. btw down to contributiemarge.

    Cost rows with a zero amount are skipped (a Shopify order has no
    bol-commissie and vice versa). A negative margin bar turns red and
    extends left of the zero line.
    """
    if breakdown.revenue_excl_cents <= 0:
        return _empty_svg("Nog geen betaalde orders in deze periode.", width=width)

    cost_rows = [
        (label, value)
        for label, value in (
            ("Inkoop (COGS)", breakdown.cogs_cents),
            ("Verzending", breakdown.shipping_cents),
            ("Betaalfees", breakdown.payment_fee_cents),
            ("bol-commissie", breakdown.bol_commission_cents),
        )
        if value != 0
    ]
    margin = breakdown.margin_cents

    row_h, pad_top, pad_bottom = 34.0, 26.0, 12.0
    n_rows = len(cost_rows) + 2  # revenue + costs + margin
    height = int(pad_top + n_rows * row_h + pad_bottom)
    label_x, plot_x, plot_end = 168.0, 178.0, width - 96.0

    lo = min(0, margin)
    hi = breakdown.revenue_excl_cents
    span = (hi - lo) or 1

    def x_at(value: float) -> float:
        return plot_x + (plot_end - plot_x) * (value - lo) / span

    parts = [_svg_open(width, height, title)]
    parts.append(
        f'<text x="{plot_x:.1f}" y="16" font-size="11" font-weight="600" '
        f'fill="{_INK}">{escape(title)}</text>'
    )
    zero_x = x_at(0)
    parts.append(
        f'<line x1="{zero_x:.1f}" y1="{pad_top:.1f}" x2="{zero_x:.1f}" '
        f'y2="{height - pad_bottom:.1f}" stroke="{_BASELINE}" stroke-width="1"/>'
    )

    def bar(row: int, label: str, lo_v: float, hi_v: float, amount: int, color: str) -> None:
        y = pad_top + row * row_h + 5
        x1, x2 = x_at(lo_v), x_at(hi_v)
        bar_width = max(x2 - x1, 1.0)
        title_el = f"<title>{escape(label)}: {escape(fmt_eur(amount))}</title>"
        parts.append(
            f'<text x="{label_x:.1f}" y="{y + 16:.1f}" text-anchor="end" '
            f'font-size="11" fill="{_INK}">{escape(label)}</text>'
        )
        parts.append(
            f'<rect x="{x1:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="22" rx="3" '
            f'fill="{color}">{title_el}</rect>'
        )
        parts.append(
            f'<text x="{x2 + 6:.1f}" y="{y + 16:.1f}" font-size="11" '
            f'fill="{_INK}">{escape(fmt_eur(amount))}</text>'
        )

    bar(0, "Omzet excl. btw", 0, breakdown.revenue_excl_cents, breakdown.revenue_excl_cents, SERIES_COLORS[0])
    cumulative = breakdown.revenue_excl_cents
    for i, (label, value) in enumerate(cost_rows):
        bar(i + 1, f"− {label}", cumulative - value, cumulative, value, SERIES_COLORS[1])
        cumulative -= value
    margin_color = _GOOD if margin >= 0 else _BAD
    bar(n_rows - 1, "Contributiemarge", min(0, margin), max(0, margin), margin, margin_color)

    parts.append("</svg>")
    return "".join(parts)
