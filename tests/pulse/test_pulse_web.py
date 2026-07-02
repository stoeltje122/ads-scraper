"""Dashboard smoke tests: every page renders, mutations work, CSRF holds."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers_pulse import PULSE_FIXTURE_DIR, REPO_ROOT
from pulse import analysis, collector, store
from pulse.db import open_db
from pulse.sources import manual
from pulse.web.app import create_app


@pytest.fixture
def client(pulse_settings) -> TestClient:
    """A dashboard on top of a demo-state database at pulse_settings.db_path."""
    conn = open_db(pulse_settings.db_path)
    store.seed_sources(conn)
    store.seed_themes(conn, REPO_ROOT / "pulse-taxonomy.seed.yaml")
    store.seed_competitors(conn, REPO_ROOT / "competitors.seed.yaml")
    collector.collect(conn, pulse_settings, fixture_dir=PULSE_FIXTURE_DIR)
    collector.import_items(
        conn, manual.parse_file(PULSE_FIXTURE_DIR / "competitor_reviews.csv")
    )
    analysis.analyze_pending(
        conn, pulse_settings, analyzer=analysis.CannedAnalyzer(PULSE_FIXTURE_DIR)
    )
    conn.close()
    return TestClient(create_app(pulse_settings))


@pytest.mark.parametrize(
    "path", ["/urgent", "/inbox", "/trends", "/kansen", "/import", "/beheer", "/item/1"]
)
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "Pulse" in resp.text


def test_root_redirects_to_urgent_when_open(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/urgent"


def test_urgent_page_shows_health_signal(client):
    text = client.get("/urgent").text
    assert "Hartkloppingen" in text and "gezondheid" in text


def test_inbox_filters_via_query(client):
    resp = client.get("/inbox", params={"thema": "bijwerking-gezondheid"})
    assert "6 item(s)" in resp.text


def test_follow_up_roundtrip(client):
    resp = client.post(
        "/item/1/opvolgen", data={"done": "1", "next": "/urgent"}, follow_redirects=False
    )
    assert resp.status_code == 303
    resp = client.post(
        "/item/1/opvolgen", data={"done": "0", "next": "/urgent"}, follow_redirects=False
    )
    assert resp.status_code == 303


def test_open_redirect_is_blocked(client):
    resp = client.post(
        "/item/1/opvolgen",
        data={"done": "1", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/urgent"


def test_cross_origin_post_rejected(client):
    resp = client.post(
        "/item/1/opvolgen", data={"done": "1"},
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_paste_import_and_dedupe(client):
    data = {"tekst": "Nieuwe testreview\n---\nNog eentje", "kanaal": "trustpilot",
            "concurrent": "Cloudpillo", "datum": "2026-07-01"}
    resp = client.post("/import/plakken", data=data, follow_redirects=False)
    assert "ok=2" in resp.headers["location"]
    resp = client.post("/import/plakken", data=data, follow_redirects=False)
    assert "ok=0" in resp.headers["location"] and "dup=2" in resp.headers["location"]


def test_csv_upload(client):
    csv_bytes = "tekst,datum\nUpload testje,2026-07-01\n".encode("utf-8")
    resp = client.post(
        "/import/csv", files={"bestand": ("test.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert "ok=1" in resp.headers["location"]


def test_forget_requires_confirmation(client):
    resp = client.post("/beheer/vergeten", data={"who": "x@y.nl"}, follow_redirects=False)
    assert "fout=" in resp.headers["location"]
    resp = client.post(
        "/beheer/vergeten",
        data={"who": "marijke.deboer@ziggo.nl", "bevestig": "ja"},
        follow_redirects=False,
    )
    assert "verwijderd" in resp.headers["location"]


def test_retention_validation(client):
    resp = client.post("/beheer/retentie", data={"months": "0"}, follow_redirects=False)
    assert "fout=" in resp.headers["location"]
    resp = client.post("/beheer/retentie", data={"months": "12"}, follow_redirects=False)
    assert "m=" in resp.headers["location"]


def test_theme_add_duplicate_is_friendly(client):
    from urllib.parse import unquote

    resp = client.post(
        "/beheer/theme/add", data={"slug": "overig"}, follow_redirects=False
    )
    assert "bestaat al" in unquote(resp.headers["location"])
