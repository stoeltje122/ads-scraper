"""Server-rendered inline SVG chart of active-ad volume over time.

No JS chart libraries: the dashboard must work offline forever. Hover
detail comes from native SVG <title> tooltips. Colors follow the light
dashboard theme (single series, validated for contrast on the surface).
"""

from __future__ import annotations

import sqlite3
from xml.sax.saxutils import escape

# Chart chrome (light theme).
_SURFACE = "#fcfcfb"
_GRID = "#e1e0d9"
_BASELINE = "#c3c2b7"
_MUTED_INK = "#898781"
_SERIES = "#2a78d6"  # single series; >= 3:1 on the light surface

_BAR_RADIUS = 4.0  # rounded data-end, anchored to the baseline


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


def volume_chart_svg(
    rows: list[sqlite3.Row], width: int = 680, height: int = 200
) -> str:
    """Bar chart of active-ad count per snapshot day.

    `rows` come from queries.volume_series: (seen_at, active). Returns an
    SVG string ('' when there is no history yet).
    """
    if not rows:
        return ""
    pad_left, pad_right, pad_top, pad_bottom = 36.0, 10.0, 14.0, 26.0
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom

    days = [str(r["seen_at"])[:10] for r in rows]
    values = [int(r["active"] or 0) for r in rows]
    max_v = max(values) or 1
    n = len(values)
    step = inner_w / n
    bar_w = max(2.0, min(26.0, step - 2.0))  # keep a 2px surface gap between bars

    parts: list[str] = [
        f'<svg class="volume-chart" viewBox="0 0 {width} {height}" '
        f'width="100%" role="img" preserveAspectRatio="xMinYMid meet" '
        f'aria-label="Aantal actieve advertenties per dag">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_SURFACE}"/>',
    ]

    # Hairline gridlines at 0 / half / max, with muted value labels.
    for frac, label in ((0.0, "0"), (0.5, str(round(max_v / 2, 1)).rstrip("0").rstrip(".")), (1.0, str(max_v))):
        y = pad_top + inner_h * (1 - frac)
        color = _BASELINE if frac == 0.0 else _GRID
        parts.append(
            f'<line x1="{pad_left:.1f}" y1="{y:.1f}" x2="{width - pad_right:.1f}" '
            f'y2="{y:.1f}" stroke="{color}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(label)}</text>'
        )

    # Bars, each with a native tooltip.
    for i, (day, value) in enumerate(zip(days, values)):
        title = f"<title>{escape(day)}: {value} actief</title>"
        x = pad_left + i * step + (step - bar_w) / 2
        if value <= 0:
            # Invisible hover target on the baseline so the day stays inspectable.
            parts.append(
                f'<rect x="{x:.1f}" y="{pad_top + inner_h - 2:.1f}" width="{bar_w:.1f}" '
                f'height="2" fill="transparent">{title}</rect>'
            )
            continue
        h = inner_h * value / max_v
        y = pad_top + inner_h - h
        parts.append(f'<path d="{_bar_path(x, y, bar_w, h)}" fill="{_SERIES}">{title}</path>')

    # First and last day on the x-axis (muted, selective — never every tick).
    y_label = height - pad_bottom + 16
    parts.append(
        f'<text x="{pad_left:.1f}" y="{y_label:.1f}" font-size="10" '
        f'fill="{_MUTED_INK}">{escape(days[0])}</text>'
    )
    if n > 1:
        parts.append(
            f'<text x="{width - pad_right:.1f}" y="{y_label:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_MUTED_INK}">{escape(days[-1])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
