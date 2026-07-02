"""Pulse dashboard: FastAPI app factory.

Server-rendered Jinja2 pages, GET filter forms, POST-redirect-GET for
mutations. One fresh sqlite connection per request (opened and closed by
a dependency). No CDN assets: one hand-written CSS file, inline SVG charts.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pulse import collector, health, queries, store
from pulse.config import Settings, load_settings
from pulse.db import open_db
from pulse.queries import ItemFilter
from pulse.sources import manual
from pulse.sources.base import SourceError
from pulse.web.chart import sentiment_chart_svg, volume_chart_svg

WEB_DIR = Path(__file__).parent
PAGE_SIZE = 50

SENTIMENT_LABELS = {"positive": "positief", "neutral": "neutraal", "negative": "negatief"}
URGENCY_LABELS = {"urgent": "urgent", "normal": "normaal", "low": "laag"}
TYPE_LABELS = {
    "complaint": "klacht",
    "question": "vraag",
    "compliment": "compliment",
    "suggestion": "suggestie",
    "review": "review",
}
PERIODS = [(7, "laatste 7 dagen"), (30, "laatste 30 dagen"), (90, "laatste 90 dagen"), (365, "laatste jaar")]
PASTE_CHANNELS = [
    ("", "Overig / handmatig"),
    ("trustpilot", "Trustpilot"),
    ("bol", "bol"),
    ("social", "Facebook/Instagram"),
    ("email", "E-mail"),
]


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _fromjson(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _qs(params: dict, **overrides) -> str:
    """Query string from current filter values, with overrides (pagination)."""
    merged = {**params, **overrides}
    clean = {k: v for k, v in merged.items() if v not in (None, "")}
    return "?" + urlencode(clean)


def _safe_next(value: str | None, fallback: str = "/urgent") -> str:
    """Only same-site paths as redirect target (no open redirect).

    Backslashes are rejected too: browsers treat '/\\evil.com' like
    '//evil.com' (protocol-relative)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return fallback


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Pulse", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """Reject cross-site POSTs (CSRF guard for a localhost tool)."""
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return Response("Cross-site POST geweigerd.", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    # Weekly reports are plain files; serve them so Beheer can link to them.
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/rapporten", StaticFiles(directory=settings.reports_dir), name="rapporten")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals.update(
        qs=_qs,
        sentiment_labels=SENTIMENT_LABELS,
        urgency_labels=URGENCY_LABELS,
        type_labels=TYPE_LABELS,
        status_labels=store.STATUS_LABELS_NL,
        periods=PERIODS,
        paste_channels=PASTE_CHANNELS,
        theme_label=lambda slug: (slug or "").replace("-", " "),
        health_keywords=health.matched_keywords,
    )
    templates.env.filters["day"] = lambda v: (v or "")[:10]
    templates.env.filters["fromjson"] = _fromjson

    async def get_conn() -> AsyncIterator[sqlite3.Connection]:
        conn = open_db(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, context: dict):
        conn = context.pop("_conn", None)
        if conn is not None:
            context.setdefault("urgent_count", queries.count_urgent_open(conn))
        return templates.TemplateResponse(request, name, context)

    # ── Start & Urgent ───────────────────────────────────────────────

    @app.get("/")
    async def index(conn: sqlite3.Connection = Depends(get_conn)):
        """Urgent is the start screen whenever something is open."""
        if queries.count_urgent_open(conn):
            return RedirectResponse("/urgent", status_code=303)
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/urgent")
    async def urgent(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        show_all = request.query_params.get("alles") == "1"
        items = queries.urgent_items(conn, include_followed_up=show_all)
        return render(
            request,
            "urgent.html",
            {
                "_conn": conn,
                "nav": "urgent",
                "items": items,
                "show_all": show_all,
                "n_open": sum(1 for i in items if not i["followed_up_at"]),
            },
        )

    @app.post("/item/{item_id}/opvolgen")
    async def follow_up(
        item_id: int,
        done: int = Form(1),
        next: str = Form("/urgent"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if not queries.get_item(conn, item_id):
            raise HTTPException(404)
        store.mark_followed_up(conn, item_id, done=bool(done))
        return RedirectResponse(_safe_next(next), status_code=303)

    # ── Inbox ────────────────────────────────────────────────────────

    @app.get("/inbox")
    async def inbox(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        p = request.query_params
        page = max(1, _int_or_none(p.get("page")) or 1)
        params = {
            "bron": _clean(p.get("bron")),
            "sentiment": _clean(p.get("sentiment")),
            "thema": _clean(p.get("thema")),
            "type": _clean(p.get("type")),
            "urgentie": _clean(p.get("urgentie")),
            "over": _clean(p.get("over")),
            "periode": _clean(p.get("periode")),
            "q": _clean(p.get("q")),
        }
        flt = ItemFilter(
            source_id=_int_or_none(params["bron"]),
            sentiment=params["sentiment"] if params["sentiment"] in SENTIMENT_LABELS else None,
            theme=params["thema"],
            type_=params["type"] if params["type"] in TYPE_LABELS else None,
            urgency=params["urgentie"] if params["urgentie"] in URGENCY_LABELS else None,
            about=params["over"],
            q=params["q"],
            days=_int_or_none(params["periode"]),
        )
        total = queries.count_items(conn, flt)
        items = queries.list_items(conn, flt, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        return render(
            request,
            "inbox.html",
            {
                "_conn": conn,
                "nav": "inbox",
                "items": items,
                "total": total,
                "page": page,
                "total_pages": max(1, math.ceil(total / PAGE_SIZE)),
                "params": params,
                "sources": store.list_sources(conn),
                "themes": store.list_themes(conn),
                "competitors": store.list_competitors(conn),
            },
        )

    # ── Item detail ──────────────────────────────────────────────────

    @app.get("/item/{item_id}")
    async def item_detail(
        item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        item = queries.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, "Item niet gevonden.")
        thread = []
        if item["thread_id"]:
            thread = queries.thread_items(conn, item["thread_id"])
            store.mark_thread_seen(conn, item["thread_id"])
        return render(
            request,
            "item_detail.html",
            {
                "_conn": conn,
                "nav": "inbox",
                "item": item,
                "thread": thread,
                "back": _safe_next(request.query_params.get("terug"), "/inbox"),
            },
        )

    # ── Trends ───────────────────────────────────────────────────────

    @app.get("/trends")
    async def trends(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        wow = queries.week_over_week(conn)
        theme_rows = queries.theme_counts(conn, only_own=True)
        max_theme = max((t["count"] for t in theme_rows), default=1)
        return render(
            request,
            "trends.html",
            {
                "_conn": conn,
                "nav": "trends",
                "volume_svg": volume_chart_svg(queries.items_per_week(conn)),
                "sentiment_svg": sentiment_chart_svg(queries.sentiment_per_week(conn)),
                "themes": theme_rows,
                "max_theme": max_theme,
                "wow": wow,
            },
        )

    # ── Kansen (concurrenten) ────────────────────────────────────────

    @app.get("/kansen")
    async def kansen(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        insights = queries.competitor_insights(conn)
        return render(
            request,
            "kansen.html",
            {
                "_conn": conn,
                "nav": "kansen",
                "insights": [i for i in insights if i["n_items"]],
                "empty_competitors": [i for i in insights if not i["n_items"]],
            },
        )

    # ── Import ───────────────────────────────────────────────────────

    @app.get("/import")
    async def import_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(
            request,
            "import.html",
            {
                "_conn": conn,
                "nav": "import",
                "competitors": store.list_competitors(conn),
                "ok": _int_or_none(request.query_params.get("ok")),
                "dup": _int_or_none(request.query_params.get("dup")),
                "error": _clean(request.query_params.get("fout")),
            },
        )

    def _import_and_redirect(conn: sqlite3.Connection, items) -> RedirectResponse:
        result = collector.import_items(conn, items)
        dup = result.items_seen - result.items_new
        return RedirectResponse(
            f"/import?ok={result.items_new}&dup={dup}", status_code=303
        )

    @app.post("/import/plakken")
    async def import_paste(
        tekst: str = Form(""),
        kanaal: str = Form(""),
        concurrent: str = Form(""),
        datum: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            items = manual.parse_paste(
                tekst, competitor=concurrent or None,
                channel_label=kanaal or None, date=datum or None,
            )
        except SourceError as exc:
            return RedirectResponse(f"/import?fout={exc}", status_code=303)
        return _import_and_redirect(conn, items)

    @app.post("/import/csv")
    async def import_csv(
        bestand: UploadFile = File(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        raw = await bestand.read()
        try:
            items = manual.parse_csv(raw.decode("utf-8-sig", errors="replace"))
        except SourceError as exc:
            return RedirectResponse(f"/import?fout={exc}", status_code=303)
        return _import_and_redirect(conn, items)

    # ── Beheer ───────────────────────────────────────────────────────

    @app.get("/beheer")
    async def beheer(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        digests = conn.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 10").fetchall()
        return render(
            request,
            "beheer.html",
            {
                "_conn": conn,
                "nav": "beheer",
                "sources": store.list_sources(conn),
                "competitors": store.list_competitors(conn),
                "themes": store.list_themes(conn, include_archived=True),
                "retention": store.retention_months(conn),
                "summary": queries.counts_summary(conn),
                "failed": queries.failed_analyses(conn),
                "runs": queries.last_runs(conn, limit=8),
                "digests": digests,
                "msg": _clean(request.query_params.get("m")),
                "error": _clean(request.query_params.get("fout")),
            },
        )

    @app.post("/beheer/source/{source_id}/status")
    async def source_status(
        source_id: int, status: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        try:
            store.set_source_status(conn, source_id, status)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse("/beheer?m=Bron bijgewerkt.", status_code=303)

    @app.post("/beheer/competitor/add")
    async def competitor_add(
        name: str = Form(...),
        category: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        name = name.strip()
        if not name:
            return RedirectResponse("/beheer?fout=Naam is verplicht.", status_code=303)
        if store.get_competitor(conn, name):
            return RedirectResponse("/beheer?fout=Deze concurrent bestaat al.", status_code=303)
        store.add_competitor(conn, name, category.strip() or None)
        return RedirectResponse("/beheer?m=Concurrent toegevoegd.", status_code=303)

    @app.post("/beheer/competitor/{competitor_id}/status")
    async def competitor_status(
        competitor_id: int, status: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        try:
            store.set_competitor_status(conn, competitor_id, status)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse("/beheer?m=Concurrent bijgewerkt.", status_code=303)

    @app.post("/beheer/competitor/{competitor_id}/update")
    async def competitor_update(
        competitor_id: int,
        category: str = Form(""),
        notes: str = Form(""),
        review_urls: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        urls = [u.strip() for u in review_urls.splitlines() if u.strip()]
        store.update_competitor(
            conn, competitor_id,
            category=category.strip() or None,
            notes=notes.strip() or None,
            review_urls=urls,
        )
        return RedirectResponse("/beheer?m=Concurrent bijgewerkt.", status_code=303)

    @app.post("/beheer/theme/add")
    async def theme_add(
        slug: str = Form(...),
        description: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        slug = slug.strip().casefold().replace(" ", "-")
        if not slug:
            return RedirectResponse("/beheer?fout=Slug is verplicht.", status_code=303)
        try:
            store.add_theme(conn, slug, description.strip() or None)
        except sqlite3.IntegrityError:
            return RedirectResponse("/beheer?fout=Dit thema bestaat al.", status_code=303)
        return RedirectResponse("/beheer?m=Thema toegevoegd.", status_code=303)

    @app.post("/beheer/theme/{theme_id}/status")
    async def theme_status(
        theme_id: int, status: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        try:
            store.set_theme_status(conn, theme_id, status)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse("/beheer?m=Thema bijgewerkt.", status_code=303)

    @app.post("/beheer/retentie")
    async def retention_update(
        months: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        value = _int_or_none(months)
        if not value or value < 1 or value > 120:
            return RedirectResponse(
                "/beheer?fout=Retentie moet tussen 1 en 120 maanden liggen.", status_code=303
            )
        store.set_setting(conn, "retention_months", str(value))
        return RedirectResponse(
            "/beheer?m=Retentie bijgewerkt. Opschoning gebeurt bij de volgende collect-run.",
            status_code=303,
        )

    @app.post("/beheer/vergeten")
    async def forget_person(
        who: str = Form(...),
        bevestig: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if bevestig != "ja":
            return RedirectResponse(
                "/beheer?fout=Vink de bevestiging aan om iemand te vergeten.", status_code=303
            )
        n = store.forget(conn, who)
        return RedirectResponse(
            f"/beheer?m={n} item(s) definitief verwijderd (AVG).", status_code=303
        )

    return app
