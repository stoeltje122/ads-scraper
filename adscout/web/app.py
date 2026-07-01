"""AdScout dashboard: FastAPI app factory.

Server-rendered Jinja2 pages, GET filter forms, POST-redirect-GET for
mutations. One fresh sqlite connection per request (opened and closed by
a dependency). No CDN assets: one hand-written CSS file, inline SVG chart.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from adscout import queries, store
from adscout.config import Settings, load_settings
from adscout.db import open_db
from adscout.queries import AdFilter
from adscout.report import ad_library_url
from adscout.web.chart import volume_chart_svg

WEB_DIR = Path(__file__).parent
PAGE_SIZE = 60

FORMAT_LABELS = {
    "image": "Beeld",
    "video": "Video",
    "carousel": "Carrousel",
    "unknown": "Onbekend",
}
SORT_OPTIONS = [
    ("newest", "Nieuwste eerst"),
    ("longest", "Langstlopend eerst"),
    ("recently_stopped", "Recent gestopt"),
]
CHANGE_PERIODS = (7, 14, 30)


# ── Small parsing helpers ────────────────────────────────────────────


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


def _nl_number(value) -> str:
    """1234567 → '1.234.567' (Dutch thousands separator)."""
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", ".")


def _qs(params: dict, **overrides) -> str:
    """Query string from current filter values, with overrides (pagination)."""
    merged = {**params, **overrides}
    clean = {k: v for k, v in merged.items() if v not in (None, "")}
    return "?" + urlencode(clean)


def _group_tags(tag_rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in tag_rows:
        groups.setdefault(row["tag_group"] or "overig", []).append(row)
    return groups


def _advertiser_countries(advertisers: list[sqlite3.Row]) -> list[str]:
    return sorted(
        {
            c.strip().upper()
            for adv in advertisers
            for c in (adv["countries"] or "").split(",")
            if c.strip()
        }
    )


# ── App factory ──────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="AdScout", docs_url=None, redoc_url=None)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    # Creatives dir only exists after the first collect with downloads;
    # mount conditionally so the dashboard also runs on a fresh install.
    media_available = settings.creatives_dir.is_dir()
    if media_available:
        app.mount(
            "/media", StaticFiles(directory=settings.creatives_dir), name="media"
        )

    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    def media_url(local_path: str | None) -> str | None:
        """/media URL for a stored creative, or None when not locally available.

        local_path values are absolute or repo-relative paths into
        settings.creatives_dir; the flat dir is served by filename.
        """
        if not local_path or not media_available:
            return None
        name = Path(local_path).name
        if not (settings.creatives_dir / name).is_file():
            return None
        return f"/media/{name}"

    templates.env.globals.update(
        media_url=media_url,
        ad_library_url=ad_library_url,
        format_label=lambda fmt: FORMAT_LABELS.get(fmt, fmt or "Onbekend"),
        qs=_qs,
        sort_options=SORT_OPTIONS,
        change_periods=CHANGE_PERIODS,
    )
    templates.env.filters["day"] = lambda v: (v or "")[:10]
    templates.env.filters["fromjson"] = _fromjson
    templates.env.filters["nlnum"] = _nl_number

    async def get_conn() -> AsyncIterator[sqlite3.Connection]:
        conn = open_db(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, context: dict):
        return templates.TemplateResponse(request, name, context)

    # ── Galerij ──────────────────────────────────────────────────────

    @app.get("/")
    async def gallery(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        p = request.query_params
        page = max(1, _int_or_none(p.get("page")) or 1)
        sort = p.get("sort") if p.get("sort") in dict(SORT_OPTIONS) else "newest"
        params = {
            "advertiser": _clean(p.get("advertiser")),
            "category": _clean(p.get("category")),
            "tag": _clean(p.get("tag")),
            "format": _clean(p.get("format")),
            "status": _clean(p.get("status")),
            "country": _clean(p.get("country")),
            "min_runtime": _clean(p.get("min_runtime")),
            "max_runtime": _clean(p.get("max_runtime")),
            "after": _clean(p.get("after")),
            "before": _clean(p.get("before")),
            "q": _clean(p.get("q")),
            "sort": sort,
        }
        filt = AdFilter(
            advertiser_id=_int_or_none(params["advertiser"]),
            advertiser_category=params["category"],
            tag_id=_int_or_none(params["tag"]),
            format=params["format"],
            status=params["status"],
            country=params["country"],
            min_runtime=_int_or_none(params["min_runtime"]),
            max_runtime=_int_or_none(params["max_runtime"]),
            first_seen_after=params["after"],
            first_seen_before=params["before"],
            search=params["q"],
            sort=sort,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        ads = queries.query_ads(conn, filt)
        total = queries.count_ads(conn, filt)
        advertisers = store.list_advertisers(conn)
        return render(
            request,
            "gallery.html",
            {
                "nav": "gallery",
                "ads": ads,
                "total": total,
                "page": page,
                "total_pages": max(1, math.ceil(total / PAGE_SIZE)),
                "params": params,
                "tags_map": queries.accepted_tags_map(conn),
                "advertisers": advertisers,
                "adv_categories": store.list_categories(conn, "advertiser_category"),
                "ad_tag_categories": store.list_categories(conn, "ad_tag"),
                "countries": _advertiser_countries(advertisers),
                "formats": list(FORMAT_LABELS),
            },
        )

    # ── Winnaars ─────────────────────────────────────────────────────

    @app.get("/winners")
    async def winners(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        category = _clean(request.query_params.get("category"))
        rows = queries.winners(conn, limit=40, category=category)
        return render(
            request,
            "winners.html",
            {
                "nav": "winners",
                "winners": rows,
                "category": category,
                "adv_categories": store.list_categories(conn, "advertiser_category"),
                "tags_map": queries.accepted_tags_map(conn),
            },
        )

    # ── Veranderingen ────────────────────────────────────────────────

    @app.get("/changes")
    async def changes(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        days = _int_or_none(request.query_params.get("days")) or 7
        if days not in CHANGE_PERIODS:
            days = 7
        since = date.today() - timedelta(days=days)
        return render(
            request,
            "changes.html",
            {
                "nav": "changes",
                "days": days,
                "since": since.isoformat(),
                "new_ads": queries.new_since(conn, since),
                "stopped_ads": queries.stopped_since(conn, since),
                "tags_map": queries.accepted_tags_map(conn),
            },
        )

    # ── Adverteerder-detail ──────────────────────────────────────────

    @app.get("/advertiser/{advertiser_id}")
    async def advertiser_detail(
        advertiser_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        adv = conn.execute(
            "SELECT * FROM advertisers WHERE id = ?", (advertiser_id,)
        ).fetchone()
        if not adv:
            raise HTTPException(status_code=404, detail="Concurrent niet gevonden")
        active = queries.query_ads(
            conn,
            AdFilter(advertiser_id=advertiser_id, status="active", sort="longest", limit=1000),
        )
        stopped = queries.query_ads(
            conn,
            AdFilter(
                advertiser_id=advertiser_id,
                status="inactive",
                sort="recently_stopped",
                limit=1000,
            ),
        )
        series = queries.volume_series(conn, advertiser_id, days=90)
        return render(
            request,
            "advertiser.html",
            {
                "nav": "gallery",
                "adv": adv,
                "active_ads": active,
                "stopped_ads": stopped,
                "top_runners": active[:5],
                "chart_svg": volume_chart_svg(series),
                "pages": store.pages_for(conn, advertiser_id),
                "tags_map": queries.accepted_tags_map(conn),
            },
        )

    # ── Ad-detail ────────────────────────────────────────────────────

    @app.get("/ad/{ad_id}")
    async def ad_detail(
        ad_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        ad = queries.get_ad(conn, ad_id)
        if not ad:
            raise HTTPException(status_code=404, detail="Ad niet gevonden")

        creative_rows = queries.ad_creatives(conn, ad_id)
        creatives = [
            {
                "media_type": c["media_type"],
                "url": media_url(c["local_path"]),
                "source_url": c["source_url"],
            }
            for c in creative_rows
        ]
        has_video = any(c["media_type"] == "video" for c in creatives)
        video_thumb = next(
            (c["url"] for c in creatives if c["media_type"] == "video_thumbnail" and c["url"]),
            None,
        )

        tag_rows = queries.ad_tags_for(conn, ad_id)
        accepted_ids = {r["category_id"] for r in tag_rows if r["status"] == "accepted"}
        suggested = [r for r in tag_rows if r["status"] == "suggested"]
        return render(
            request,
            "ad_detail.html",
            {
                "nav": "gallery",
                "ad": ad,
                "texts": queries.ad_texts(conn, ad_id),
                "creatives": creatives,
                "has_video": has_video,
                "video_thumb": video_thumb,
                "sightings": queries.ad_sightings(conn, ad_id),
                "accepted_ids": accepted_ids,
                "suggested": suggested,
                "tag_groups": _group_tags(store.list_categories(conn, "ad_tag")),
                "shared_ads": queries.shared_creative_ads(conn, ad_id),
            },
        )

    @app.post("/ad/{ad_id}/tags")
    async def ad_update_tags(
        ad_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        """Sync accepted tags with the submitted checkbox set."""
        if not queries.get_ad(conn, ad_id):
            raise HTTPException(status_code=404, detail="Ad niet gevonden")
        form = await request.form()
        submitted = {int(v) for v in form.getlist("tag") if str(v).isdigit()}
        accepted = {
            r["category_id"]
            for r in queries.ad_tags_for(conn, ad_id)
            if r["status"] == "accepted"
        }
        for category_id in submitted - accepted:
            store.set_tag(conn, ad_id, category_id)
        for category_id in accepted - submitted:
            store.remove_tag(conn, ad_id, category_id)
        return RedirectResponse(f"/ad/{ad_id}", status_code=303)

    @app.post("/ad/{ad_id}/suggestion")
    async def ad_decide_suggestion(
        ad_id: str,
        category_id: int = Form(...),
        decision: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        store.decide_tag(conn, ad_id, category_id, accept=(decision == "accept"))
        return RedirectResponse(f"/ad/{ad_id}", status_code=303)

    # ── Beheer ───────────────────────────────────────────────────────

    @app.get("/beheer")
    async def beheer(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        advertisers = store.list_advertisers(conn)
        pages_by_adv = {adv["id"]: store.pages_for(conn, adv["id"]) for adv in advertisers}
        return render(
            request,
            "beheer.html",
            {
                "nav": "beheer",
                "advertisers": advertisers,
                "pages_by_adv": pages_by_adv,
                "adv_categories": store.list_categories(conn, "advertiser_category"),
                "tag_groups": _group_tags(store.list_categories(conn, "ad_tag")),
                "suggestions": queries.pending_ai_suggestions(conn),
            },
        )

    @app.post("/beheer/advertiser/add")
    async def beheer_advertiser_add(
        name: str = Form(...),
        category: str = Form(""),
        countries: str = Form("NL"),
        notes: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        name = name.strip()
        if name:
            country_list = [c.strip().upper() for c in countries.split(",") if c.strip()]
            store.add_advertiser(
                conn,
                name,
                category=_clean(category),
                countries=country_list or ["NL"],
                notes=_clean(notes),
            )
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/advertiser/{advertiser_id}/status")
    async def beheer_advertiser_status(
        advertiser_id: int,
        status: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if status in ("active", "paused"):
            store.set_advertiser_status(conn, advertiser_id, status)
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/page/add")
    async def beheer_page_add(
        advertiser_id: int = Form(...),
        page_id: str = Form(...),
        page_name: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        page_id = page_id.strip()
        if page_id:
            store.add_page(conn, advertiser_id, page_id, _clean(page_name))
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/page/remove")
    async def beheer_page_remove(
        advertiser_id: int = Form(...),
        page_id: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        store.remove_page(conn, advertiser_id, page_id)
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/category/add")
    async def beheer_category_add(
        name: str = Form(...),
        type_: str = Form(..., alias="type"),
        tag_group: str = Form(""),
        description: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        name = name.strip()
        if name and type_ in ("ad_tag", "advertiser_category"):
            store.add_category(conn, name, type_, _clean(tag_group), _clean(description))
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/category/{category_id}/rename")
    async def beheer_category_rename(
        category_id: int,
        new_name: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        new_name = new_name.strip()
        if new_name:
            store.rename_category(conn, category_id, new_name)
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/category/{category_id}/delete")
    async def beheer_category_delete(
        category_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ):
        store.delete_category(conn, category_id)
        return RedirectResponse("/beheer", status_code=303)

    @app.post("/beheer/suggestion")
    async def beheer_decide_suggestion(
        ad_id: str = Form(...),
        category_id: int = Form(...),
        decision: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        store.decide_tag(conn, ad_id, category_id, accept=(decision == "accept"))
        return RedirectResponse("/beheer", status_code=303)

    return app
