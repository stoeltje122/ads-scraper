"""Weekly digest: Markdown + a dependency-free HTML twin in reports/.

Written in plain Dutch for the founders: what came in, what is urgent (and
whether it was followed up), top complaints/compliments vs last week, a
notable trend, competitor opportunities, and recommended actions. Every
generated digest is recorded in the digests table.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from pulse import queries
from pulse.models import utc_now_iso

EXCERPT_CHARS = 160


def theme_label(slug: str) -> str:
    return slug.replace("-", " ")


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def collect_week_data(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """All numbers for one weekly report (7-day window: today-6 .. today,
    matching queries.week_over_week)."""
    today = today or date.today()
    start = today - timedelta(days=6)
    start_iso, end_iso = start.isoformat(), (today + timedelta(days=1)).isoformat()

    per_source = conn.execute(
        """SELECT s.name, COUNT(i.id) AS n
           FROM sources s LEFT JOIN items i ON i.source_id = s.id
                AND COALESCE(i.happened_at, i.first_seen) >= ?
                AND COALESCE(i.happened_at, i.first_seen) < ?
           GROUP BY s.id ORDER BY n DESC, s.name""",
        (start_iso, end_iso),
    ).fetchall()

    urgent = [
        row
        for row in queries.urgent_items(conn, include_followed_up=True)
        if (row["happened_at"] or row["first_seen"]) >= start_iso
    ]
    wow = queries.week_over_week(conn, today=today)
    insights = [
        ins for ins in queries.competitor_insights(conn) if ins["n_analyzed"] > 0
    ]
    summary = queries.counts_summary(conn)

    return {
        "period_start": start.isoformat(),
        "period_end": today.isoformat(),
        "per_source": [dict(r) for r in per_source],
        "n_new": sum(r["n"] for r in per_source),
        "urgent": urgent,
        "wow": wow,
        "insights": insights,
        "summary": summary,
        "actions": _recommended_actions(urgent, wow, insights, summary),
    }


def _recommended_actions(urgent, wow, insights, summary) -> list[str]:
    """Plain-language to-do list. Rules, no AI: predictable and free."""
    actions: list[str] = []
    open_health = [
        r for r in urgent
        if not r["followed_up_at"] and (r["health_flag"] or r["pre_health_flag"])
    ]
    open_other = [
        r for r in urgent
        if not r["followed_up_at"] and not (r["health_flag"] or r["pre_health_flag"])
    ]
    if open_health:
        actions.append(
            f"Neem vandaag contact op over de {len(open_health)} openstaande "
            "melding(en) met een mogelijk gezondheidssignaal (zie Urgent in het dashboard)."
        )
    if open_other:
        actions.append(f"Handel de {len(open_other)} overige openstaande urgente zaken af.")
    if summary["queue"]:
        actions.append(
            f"Er wachten {summary['queue']} items op AI-analyse — draai `pulse analyze`."
        )
    if summary["failed"]:
        actions.append(
            f"{summary['failed']} item(s) konden niet geanalyseerd worden en staan "
            "klaar voor heranalyse (zie `pulse status`)."
        )
    if wow["complaints_now"]:
        top = wow["complaints_now"][0]
        actions.append(
            f"Grootste klachtenthema deze week: “{theme_label(top['theme'])}” "
            f"({top['count']}×) — overweeg een vast antwoord of een fix."
        )
    for ins in insights:
        if len(ins["cons"]) >= 2:
            actions.append(
                f"Kans: bij {ins['competitor']['name']} klagen klanten herhaaldelijk "
                f"(o.a. “{_excerpt(ins['cons'][0], 80)}”) — mogelijke invalshoek voor "
                "een Cloudplunge-advertentie (zie Kansen + AdScout)."
            )
            break
    if not actions:
        actions.append("Geen openstaande acties — rustige week.")
    return actions


# ── Markdown ─────────────────────────────────────────────────────────


def render_markdown(data: dict) -> str:
    lines: list[str] = [
        f"# Pulse weekrapport — {data['period_start']} t/m {data['period_end']}",
        "",
        "## In één oogopslag",
        f"- **{data['n_new']} nieuwe items** deze week",
    ]
    for row in data["per_source"]:
        if row["n"]:
            lines.append(f"  - {row['name']}: {row['n']}")
    open_urgent = [r for r in data["urgent"] if not r["followed_up_at"]]
    lines.append(
        f"- **{len(data['urgent'])} urgente zaken** deze week, "
        f"waarvan **{len(open_urgent)} nog open**"
    )
    if data["summary"]["queue"]:
        lines.append(f"- {data['summary']['queue']} items wachten op AI-analyse")

    lines += ["", "## Urgente zaken (gezondheid eerst)"]
    if not data["urgent"]:
        lines.append("Geen urgente zaken deze week. 🎉")
    for row in data["urgent"]:
        badge = "GEZONDHEID" if (row["health_flag"] or row["pre_health_flag"]) else "URGENT"
        status = "opgevolgd ✓" if row["followed_up_at"] else "**NOG OPEN**"
        when = (row["happened_at"] or row["first_seen"])[:10]
        lines.append(
            f"- [{badge}] {when} · {row['source_name']}: "
            f"“{_excerpt(row['text'])}” — {status}"
        )

    def top3(title: str, now_key: str, prev_key: str) -> None:
        lines.extend(["", f"## {title}"])
        now, prev = data["wow"][now_key], {t["theme"]: t["count"] for t in data["wow"][prev_key]}
        if not now:
            lines.append("(geen deze week)")
        for i, t in enumerate(now, 1):
            was = prev.get(t["theme"])
            delta = f" (vorige week: {was}×)" if was else " (nieuw t.o.v. vorige week)"
            lines.append(f"{i}. {theme_label(t['theme'])} — {t['count']}×{delta}")

    top3("Top-klachten deze week", "complaints_now", "complaints_prev")
    top3("Top-complimenten deze week", "compliments_now", "compliments_prev")

    lines += ["", "## Opvallende trend", _trend_line(data["wow"])]

    lines += ["", "## Concurrent-kansen"]
    if not data["insights"]:
        lines.append("Nog geen geanalyseerde concurrent-items. Importeer reviews via Import.")
    for ins in data["insights"]:
        name = ins["competitor"]["name"]
        lines.append(f"### {name} ({ins['n_analyzed']} geanalyseerd)")
        for con in ins["cons"][:3]:
            lines.append(f"- klacht: {_excerpt(con, 120)}")
        for pro in ins["pros"][:2]:
            lines.append(f"- pluspunt: {_excerpt(pro, 120)}")
        if ins["cons"]:
            lines.append(
                f"- 💡 mogelijke advertentie-invalshoek: benadruk waar Cloudplunge "
                f"het hier beter doet (zie dashboard → Kansen)."
            )

    lines += ["", "## Aanbevolen acties"]
    for i, action in enumerate(data["actions"], 1):
        lines.append(f"{i}. {action}")
    lines += ["", "---", "_Automatisch gegenereerd door Pulse. Pulse geeft nooit "
              "medisch advies; gezondheidsmeldingen zijn markeringen voor jullie eigen opvolging._"]
    return "\n".join(lines) + "\n"


def _trend_line(wow: dict) -> str:
    now = {t["theme"]: t["count"] for t in wow["themes_now"]}
    prev = {t["theme"]: t["count"] for t in wow["themes_prev"]}
    best_theme, best_delta = None, 0
    for theme, n in now.items():
        delta = n - prev.get(theme, 0)
        if delta > best_delta:
            best_theme, best_delta = theme, delta
    if not best_theme or best_delta < 2:
        return "Geen opvallende verschuiving ten opzichte van vorige week."
    return (
        f"“{theme_label(best_theme)}” kwam deze week {best_delta}× vaker voor dan "
        f"vorige week ({now[best_theme]}× nu). Even induiken via de Inbox-filter."
    )


# ── HTML twin (dependency-free) ──────────────────────────────────────


def render_html(markdown: str) -> str:
    """Tiny Markdown→HTML for exactly the constructs render_markdown emits.
    No external dependency, works offline forever."""
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("###"):
            close_list()
            out.append(f"<h3>{_inline(stripped[3:].strip())}</h3>")
        elif stripped.startswith("##"):
            close_list()
            out.append(f"<h2>{_inline(stripped[2:].strip())}</h2>")
        elif stripped.startswith("#"):
            close_list()
            out.append(f"<h1>{_inline(stripped[1:].strip())}</h1>")
        elif stripped.startswith("- ") or re.match(r"^\d+\.\s", stripped):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = stripped[2:] if stripped.startswith("- ") else stripped.split(". ", 1)[-1]
            out.append(f"<li>{_inline(content)}</li>")
        elif stripped in ("", "---"):
            close_list()
            if stripped == "---":
                out.append("<hr>")
        else:
            close_list()
            out.append(f"<p>{_inline(stripped)}</p>")
    close_list()
    body = "\n".join(out)
    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pulse weekrapport</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto;
       padding: 0 1rem; color: #26251f; line-height: 1.55; }}
h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 1.8rem; }}
h3 {{ font-size: 1rem; margin-top: 1.2rem; }}
li {{ margin: .25rem 0; }} hr {{ border: 0; border-top: 1px solid #ddd; margin: 2rem 0; }}
em {{ color: #6b6a63; }}
</style></head><body>
{body}
</body></html>
"""


def _inline(text: str) -> str:
    escaped = html.escape(text)
    while "**" in escaped:
        escaped = escaped.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    if escaped.startswith("_") and escaped.endswith("_") and len(escaped) > 2:
        escaped = f"<em>{escaped[1:-1]}</em>"
    return escaped


def write_weekly(
    conn: sqlite3.Connection, reports_dir: Path, today: date | None = None
) -> tuple[Path, Path]:
    """Generate this week's report, register it in digests, return paths."""
    today = today or date.today()
    data = collect_week_data(conn, today=today)
    markdown = render_markdown(data)

    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"pulse-weekrapport-{today.isoformat()}.md"
    html_path = reports_dir / f"pulse-weekrapport-{today.isoformat()}.html"
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(render_html(markdown), encoding="utf-8")

    open_urgent = len([r for r in data["urgent"] if not r["followed_up_at"]])
    conn.execute(
        """INSERT INTO digests (period_start, period_end, generated_at, md_path,
                                html_path, summary_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data["period_start"],
            data["period_end"],
            utc_now_iso(),
            str(md_path),
            str(html_path),
            json.dumps(
                {"n_new": data["n_new"], "n_urgent": len(data["urgent"]),
                 "n_urgent_open": open_urgent},
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    return md_path, html_path
