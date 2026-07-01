"""CLI smoke tests via typer's CliRunner — fully offline, in a temp workspace."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ADS_FIXTURE_DIR, PAGE_IDS, REPO_ROOT, RUN_DAY

from adscout import db
from adscout.cli import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A temp cwd with seed YAMLs, so the CLI never touches the repo's data."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    for name in ("competitors.seed.yaml", "taxonomy.seed.yaml"):
        (ws / name).write_text((REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(ws)
    monkeypatch.setenv("ADSCOUT_DATA_DIR", str(ws / "data"))
    return ws


def _invoke(args: list[str]):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, f"{args} failed (exit {result.exit_code}):\n{result.output}"
    return result


def _run_collect(workspace: Path) -> None:
    _invoke(["init"])
    for name, page_id in PAGE_IDS.items():
        _invoke(["page", "add", name, page_id, "--page-name", name])
    _invoke(
        [
            "collect",
            "--source", "fixture",
            "--fixture-dir", str(ADS_FIXTURE_DIR),
            "--skip-creatives",
            "--run-date", RUN_DAY.isoformat(),
        ]
    )


def test_init_creates_db_and_seeds(workspace):
    result = _invoke(["init"])

    assert "Database klaar" in result.output
    db_path = workspace / "data" / "adscout.db"
    assert db_path.is_file()

    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM advertisers").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] > 0
    conn.close()

    # Init twice must be safe (idempotent seeding).
    _invoke(["init"])
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM advertisers").fetchone()[0] == 9
    conn.close()


def test_collect_with_fixture_source(workspace):
    _run_collect(workspace)

    conn = db.connect(workspace / "data" / "adscout.db")
    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_collect_output_reports_totals(workspace):
    _invoke(["init"])
    for name, page_id in PAGE_IDS.items():
        _invoke(["page", "add", name, page_id])
    result = _invoke(
        [
            "collect",
            "--source", "fixture",
            "--fixture-dir", str(ADS_FIXTURE_DIR),
            "--skip-creatives",
            "--run-date", RUN_DAY.isoformat(),
        ]
    )
    assert "Totaal: 6 ads, 6 nieuw, 0 gestopt" in result.output
    # Advertisers without a linked page are skipped, not failed.
    assert "overgeslagen" in result.output


def test_export_csv_and_json(workspace):
    _run_collect(workspace)

    result = _invoke(["export", "--csv", "--json"])
    assert "✓ Export:" in result.output

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    csv_path = workspace / "exports" / f"adscout-{stamp}.csv"
    json_path = workspace / "exports" / f"adscout-{stamp}.json"
    assert csv_path.is_file() and json_path.is_file()

    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6
    assert {"ad_archive_id", "advertiser", "runtime_days", "ad_library_url"} <= set(rows[0])

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload) == 6
    assert all("texts" in record for record in payload)
    exported_ids = {record["ad_archive_id"] for record in payload}
    assert "1001001001001" in exported_ids


def test_status_without_token_stays_offline(workspace):
    _invoke(["init"])

    result = _invoke(["status"])

    # Empty META_ACCESS_TOKEN: a clear message, no network attempt.
    assert "geen META_ACCESS_TOKEN" in result.output
    assert "Watchlist:" in result.output
    assert "nog geen runs" in result.output
