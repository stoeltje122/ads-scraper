"""Shared pytest fixtures for the AdScout test suite.

All tests are offline and isolated: the environment is scrubbed so a
developer's .env / data/ directory can never leak into a test run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from adscout import collector, store
from adscout.db import open_db
from adscout.sources.fixture import FixtureAdSource

TESTS_DIR = Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent
ADS_FIXTURE_DIR = TESTS_DIR / "fixtures" / "ads"

# The env date this repo's fixtures were written around. All fixture dates
# are in the past relative to this day, so assertions stay deterministic.
RUN_DAY = date(2026, 7, 1)

# page_id per advertiser as used in tests/fixtures/ads/sample_ads_archive.json
PAGE_IDS = {
    "Cloudpillo": "111222333444555",
    "Zelesta": "666777888999000",
    "8hours": "121212343434565",
}
CATEGORIES = {
    "Cloudpillo": "sleep-comfort",
    "Zelesta": "sleep-comfort",
    "8hours": "supplement",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Never touch the repo's real data/ dir or a developer's .env."""
    monkeypatch.setenv("ADSCOUT_DATA_DIR", str(tmp_path / "adscout-data"))
    for var in (
        "META_ACCESS_TOKEN",
        "META_GRAPH_VERSION",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ADSCOUT_DEFAULT_COUNTRIES",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_db(tmp_path) -> sqlite3.Connection:
    """A fresh, fully migrated database in a temporary directory."""
    conn = open_db(tmp_path / "test-adscout.db")
    yield conn
    conn.close()


def seed_watchlist(conn: sqlite3.Connection, countries: list[str] | None = None) -> None:
    """Seed the three fixture advertisers, each linked to its fixture page."""
    for name, page_id in PAGE_IDS.items():
        advertiser_id = store.add_advertiser(conn, name, CATEGORIES[name], countries or ["NL"])
        store.add_page(conn, advertiser_id, page_id, name)


@pytest.fixture
def seeded_db(tmp_db) -> sqlite3.Connection:
    seed_watchlist(tmp_db)
    return tmp_db


@pytest.fixture
def fixture_source() -> FixtureAdSource:
    return FixtureAdSource(ADS_FIXTURE_DIR)


def sample_items() -> list[dict]:
    """Fresh copies of the committed fixture ads (safe to mutate per test)."""
    payload = json.loads(
        (ADS_FIXTURE_DIR / "sample_ads_archive.json").read_text(encoding="utf-8")
    )
    return payload["data"]


@pytest.fixture
def make_fixture_dir(tmp_path):
    """Factory: write a list of raw ad items as a fixture dir for FixtureAdSource."""

    def _make(items: list[dict], name: str = "fixture-ads") -> Path:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ads.json").write_text(
            json.dumps({"data": items}, ensure_ascii=False), encoding="utf-8"
        )
        return directory

    return _make


@pytest.fixture
def run_collect():
    """Helper to run one collect pass with a default deterministic run date."""

    def _run(conn: sqlite3.Connection, source, run_date: date = RUN_DAY):
        return collector.collect(conn, source, run_date=run_date)

    return _run


@pytest.fixture
def collected_db(seeded_db, fixture_source, run_collect):
    """Seeded database after one collect run on RUN_DAY (plus the run result)."""
    result = run_collect(seeded_db, fixture_source)
    return seeded_db, result
