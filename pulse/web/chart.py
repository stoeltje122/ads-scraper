"""Server-rendered inline SVG charts for the Trends page.

No JS chart libraries: the dashboard must work offline forever. Hover
detail comes from native SVG <title> tooltips. Same design language as
AdScout's chart (light surface, hairline grid, rounded bar tops).
"""

from __future__ import annotations

from xml.sax.saxutils import escape

# Chart chrome (light theme).
_SURFACE = "#fcfcfb"
_GRID = "#e1e0d9"
_BASELINE = "#c3c2b7"
_MUTED_INK = "#898781"
_SERIES = "#0f766e"  # Pulse accent (teal); >= 3:1 on the light surface

# Sentiment series — colorblind-safe trio, validated on the light surface.
_POSITIVE = "#1a7f37"
_NEUTRAL = "#8a8878"
_NEGATIVE = "#b3261e"

_BAR_RADIUS = 4.0


def _bar_path(x: float, y: float, w: float, h: float) -> str:
    """Bar with rounded top corners, flat at the baseline."""
    r = min(_BAR_RADIUS, w / 2, h)
    return (
        f"M{x:.1f},{y + h:.1f} L{x:.1f},{y + r:.1f} "
        f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"L{x + w - r:.1f},{y:.1f} "
        f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"L{x + w:.1f},{y + h:.1f} Z"
    )


def _frame(width: int, height: int, max_v: int, pads: tuple, label: str) -> list[str]:
    pad_left, pad_right, pad_top, pad_bottom = pads
    inner_h = height - pad_top - pad_bottom
    parts = [
        f'<svg class="trend-chart" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" preserveAspectRatio="xMinYMid meet" aria-label="{escape(label)}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_SURFACE}"/>',
    ]
    half = str(round(max_v / 2, 1)).rstrip("0").rstrip(".")
    for frac, text in ((0.0, "0"), (0.5, half), (1.0, str(max_v))):
        y = pad_top + inner_h * (1 - frac)
        color = _BASELINE if frac == 0.0 else _GRID
        parts.append(
            f'<line x1="{pad_left:.1f}" y1="{y:.1f}" x2="{width - pad_right:.1f}" '
            f'y2="{y:.1f}" stroke="{color}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(text)}</text>'
        )
    return parts


def _x_labels(parts: list[str], labels: list[str], width: int, height: int, pads: tuple) -> None:
    pad_left, pad_right, _, pad_bottom = pads
    y = height - pad_bottom + 16
    parts.append(
        f'<text x="{pad_left:.1f}" y="{y:.1f}" font-size="10" '
        f'fill="{_MUTED_INK}">{escape(labels[0])}</text>'
    )
    if len(labels) > 1:
        parts.append(
            f'<text x="{width - pad_right:.1f}" y="{y:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(labels[-1])}</text>'
        )


def volume_chart_svg(rows: list[dict], width: int = 680, height: int = 190) -> str:
    """Bar chart of item count per week. rows: [{'week': ..., 'n': ...}]."""
    if not rows:
        return ""
    pads = (36.0, 10.0, 14.0, 26.0)
    pad_left, pad_right, pad_top, pad_bottom = pads
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    values = [int(r["n"] or 0) for r in rows]
    labels = [str(r["week"]) for r in rows]
    max_v = max(values) or 1
    step = inner_w / len(values)
    bar_w = max(2.0, min(34.0, step - 4.0))

    parts = _frame(width, height, max_v, pads, "Aantal feedback-items per week")
    for i, (label, value) in enumerate(zip(labels, values)):
        title = f"<title>{escape(label)}: {value} items</title>"
        x = pad_left + i * step + (step - bar_w) / 2
        if value <= 0:
            parts.append(
                f'<rect x="{x:.1f}" y="{pad_top + inner_h - 2:.1f}" width="{bar_w:.1f}" '
                f'height="2" fill="transparent">{title}</rect>'
            )
            continue
        h = inner_h * value / max_v
        parts.append(
            f'<path d="{_bar_path(x, pad_top + inner_h - h, bar_w, h)}" '
            f'fill="{_SERIES}">{title}</path>'
        )
    _x_labels(parts, labels, width, height, pads)
    parts.append("</svg>")
    return "".join(parts)


def sentiment_chart_svg(rows: list[dict], width: int = 680, height: int = 190) -> str:
    """Stacked bars per week: positive (bottom), neutral, negative (top).
    rows: [{'week', 'positive', 'neutral', 'negative'}]."""
    if not rows:
        return ""
    pads = (36.0, 10.0, 14.0, 26.0)
    pad_left, pad_right, pad_top, pad_bottom = pads
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    labels = [str(r["week"]) for r in rows]
    totals = [r["positive"] + r["neutral"] + r["negative"] for r in rows]
    max_v = max(totals) or 1
    step = inner_w / len(rows)
    bar_w = max(2.0, min(34.0, step - 4.0))

    parts = _frame(width, height, max_v, pads, "Sentiment per week")
    for i, row in enumerate(rows):
        x = pad_left + i * step + (step - bar_w) / 2
        y = pad_top + inner_h
        title = (
            f"<title>{escape(labels[i])}: {row['positive']} positief, "
            f"{row['neutral']} neutraal, {row['negative']} negatief</title>"
        )
        for key, color in (("positive", _POSITIVE), ("neutral", _NEUTRAL), ("negative", _NEGATIVE)):
            value = row[key]
            if value <= 0:
                continue
            h = inner_h * value / max_v
            y -= h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{color}">{title}</rect>'
            )
    _x_labels(parts, labels, width, height, pads)
    parts.append("</svg>")
    return "".join(parts)
