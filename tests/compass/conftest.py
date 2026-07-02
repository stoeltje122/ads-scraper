"""Shared Compass test fixtures.

The autouse fixture guarantees a developer's .env / data/ directory can
never leak into a test run (same policy as the AdScout suite, extended
with the Compass-specific variables).
"""

from __future__ import annotations

from datetime import date

import pytest

from compass.db import open_db

# Fixed deterministic "today" for tests: all fixture data is generated
# relative to a date, so assertions stay stable no matter when tests run.
RUN_DAY = date(2026, 7, 1)

_ENV_VARS = [
    "COMPASS_DATA_DIR",
    "SHOPIFY_SHOP",
    "SHOPIFY_ACCESS_TOKEN",
    "SHOPIFY_API_VERSION",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_GRAPH_VERSION",
    "BOL_CLIENT_ID",
    "BOL_CLIENT_SECRET",
]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path / "data"))
    for var in _ENV_VARS[1:]:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def tmp_db(tmp_path):
    conn = open_db(tmp_path / "test-compass.db")
    yield conn
    conn.close()
