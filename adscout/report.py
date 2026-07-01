"""Weekly report: what changed in the last 7 days, and who is winning.

Output: Markdown (always) and a simple standalone HTML twin, written to
reports/. The Notifier interface (notify.py) can later push these to
e-mail/Slack without touching this module.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from adscout import queries


def build_weekly_markdown(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    since = today - timedelta(days=7)
    lines: list[str] = []
    add = lines.append

    add(f"# AdScout weekrapport — {today.isoformat()}")
    add("")
    add(f"Periode: {since.isoformat()} t/m {today.isoformat()}. "
        "Maatstaf voor 'winnaars' is looptijd: Meta geeft geen spend/impressies "
        "voor commerciële ads, dus lang doorlopen = beste beschikbare proxy voor succes.")
    add("")

    # Per advertiser: new / stopped / active
    add("## Per concurrent")
    add("")
    add("| Concurrent | Categorie | Actief nu | Nieuw (7d) | Gestopt (7d) | Totaal bekend |")
    add("|---|---|---:|---:|---:|---:|")
    stats = queries.advertiser_week_stats(conn, since)
    for row in stats:
        paused = " *(gepauzeerd)*" if row["advertiser_status"] == "paused" else ""
        add(
            f"| {row['name']}{paused} | {row['category'] or '-'} | {row['active_now'] or 0} "
            f"| {row['new_ads'] or 0} | {row['stopped_ads'] or 0} | {row['total_ads'] or 0} |"
        )
    add("")

    # Notable volume changes
    notable = [
        r for r in stats
        if (r["new_ads"] or 0) + (r["stopped_ads"] or 0) >= 5
        or ((r["active_now"] or 0) > 0 and (r["new_ads"] or 0) >= max(2, (r["active_now"] or 0) * 0.3))
    ]
    if notable:
        add("## Opvallende volume-veranderingen")
        add("")
        for r in notable:
            add(f"- **{r['name']}**: {r['new_ads'] or 0} nieuw en "
                f"{r['stopped_ads'] or 0} gestopt deze week (nu {r['active_now'] or 0} actief).")
        add("")

    # Top runners per advertiser category
    add("## Top 5 langstlopers per categorie (actieve ads)")
    add("")
    cats = sorted({r["category"] for r in stats if r["category"]})
    for cat in cats:
        top = queries.winners(conn, limit=5, category=cat)
        if not top:
            continue
        add(f"### {cat}")
        add("")
        for ad in top:
            days = ad["runtime_days"]
            body = (ad["first_body"] or "").replace("\n", " ")[:110]
            add(
                f"- **{ad['advertiser_name']}** — {days if days is not None else '?'} dagen, "
                f"{ad['format']}, gestart {(_day(ad['ad_delivery_start']) or '?')} — "
                f"[Ad Library]({ad_library_url(ad['ad_archive_id'])})"
                + (f"\n  > {body}…" if body else "")
            )
        add("")

    # New / stopped lists (capped)
    new = queries.new_since(conn, since, limit=40)
    stopped = queries.stopped_since(conn, since, limit=40)
    add(f"## Nieuw deze week ({len(new)}{'+' if len(new) == 40 else ''})")
    add("")
    for ad in new:
        add(f"- {ad['advertiser_name']}: {ad['format']}, eerst gezien {ad['first_seen']} — "
            f"[Ad Library]({ad_library_url(ad['ad_archive_id'])})")
    if not new:
        add("*Geen nieuwe ads gezien.*")
    add("")
    add(f"## Gestopt deze week ({len(stopped)}{'+' if len(stopped) == 40 else ''})")
    add("")
    for ad in stopped:
        days = ad["runtime_days"]
        add(f"- {ad['advertiser_name']}: liep {days if days is not None else '?'} dagen — "
            f"[Ad Library]({ad_library_url(ad['ad_archive_id'])})")
    if not stopped:
        add("*Geen gestopte ads gezien.*")
    add("")
    add("---")
    add("*Gegenereerd door AdScout. Creatives en data: uitsluitend intern gebruik.*")
    return "\n".join(lines)


def write_weekly(
    conn: sqlite3.Connection, reports_dir: Path, today: date | None = None
) -> tuple[Path, Path]:
    today = today or date.today()
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = build_weekly_markdown(conn, today)
    md_path = reports_dir / f"weekly-{today.isoformat()}.md"
    md_path.write_text(md, encoding="utf-8")
    html_path = reports_dir / f"weekly-{today.isoformat()}.html"
    html_path.write_text(_markdown_to_html(md), encoding="utf-8")
    return md_path, html_path


def ad_library_url(ad_archive_id: str) -> str:
    """Durable public link to the ad in Meta's Ad Library web UI.

    More durable than ad_snapshot_url, which embeds an expiring token.
    """
    return f"https://www.facebook.com/ads/library/?id={ad_archive_id}"


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


def _markdown_to_html(md: str) -> str:
    """Tiny, dependency-free markdown-to-HTML for our own report structure."""
    out: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>AdScout weekrapport</title>",
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
    import re

    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text
