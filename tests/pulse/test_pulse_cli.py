"""CLI end-to-end on fixtures: init → demo → status → export → forget."""

from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from helpers_pulse import PULSE_FIXTURE_DIR, REPO_ROOT
from pulse.cli import app

runner = CliRunner()


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A temp working directory that looks like the repo root (seeds +
    fixtures present), with PULSE_DATA_DIR pointing inside it."""
    for seed in ("pulse-taxonomy.seed.yaml", "competitors.seed.yaml"):
        shutil.copy(REPO_ROOT / seed, tmp_path / seed)
    fixture_target = tmp_path / "tests" / "fixtures" / "pulse"
    shutil.copytree(PULSE_FIXTURE_DIR, fixture_target)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_init_seeds_everything(project_dir):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "Bronnen geseed: 5" in result.output
    assert "Thema's geseed: 15" in result.output
    assert "9 concurrenten" in result.output


def test_demo_flow_and_status(project_dir):
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "25 items" in result.output
    assert "Weekrapport" in result.output

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0
    assert "35 geanalyseerd" in status.output
    assert "urgente melding" in status.output

    sources = runner.invoke(app, ["sources"])
    assert "Support-mail (Gmail)" in sources.output
    assert "wacht op configuratie" in sources.output


def test_collect_requires_init(project_dir):
    result = runner.invoke(app, ["collect"])
    assert result.exit_code == 2
    assert "pulse init" in result.output


def test_import_and_forget(project_dir):
    runner.invoke(app, ["init"])
    csv_file = project_dir / "reviews.csv"
    csv_file.write_text(
        "tekst,auteur,datum\nPrima spul,tester@voorbeeld.nl,2026-06-30\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["import", str(csv_file)])
    assert result.exit_code == 0 and "1 items geïmporteerd" in result.output
    # importing again: dedupe
    result = runner.invoke(app, ["import", str(csv_file)])
    assert "0 items geïmporteerd (1 al bekend" in result.output

    result = runner.invoke(app, ["forget", "tester@voorbeeld.nl", "--yes"])
    assert result.exit_code == 0 and "1 item(s) verwijderd" in result.output
    result = runner.invoke(app, ["forget", "tester@voorbeeld.nl", "--yes"])
    assert "Geen items gevonden" in result.output


def test_export_csv_and_json(project_dir):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["demo"])
    result = runner.invoke(app, ["export", "--csv", "--json"])
    assert result.exit_code == 0, result.output
    exports = list((project_dir / "exports").iterdir())
    assert len(exports) == 2
    json_file = next(p for p in exports if p.suffix == ".json")
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(payload) == 35
    assert payload[0]["bron"]


def test_verify_fixtures_all_green(project_dir):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["verify", "--fixtures"])
    assert result.exit_code == 0, result.output
    assert result.output.count("✓") == 5


def test_verify_without_credentials_is_honest(project_dir):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "✗ gmail" in result.output
    assert "✓ manual" in result.output
    assert "handmatige import" in result.output.casefold()


def test_analyze_without_key_friendly_message(project_dir):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_report_weekly(project_dir):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["demo"])
    result = runner.invoke(app, ["report", "--weekly"])
    assert result.exit_code == 0
    assert "Rapport geschreven" in result.output
