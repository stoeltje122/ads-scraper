"""CLI + verify smoke tests via typer's CliRunner — fully offline, in a
temp workspace. The verify tests drive compass.verify.run() directly from
the repo root so the committed fixture files are found."""

from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT, RUN_DAY, TESTS_DIR

from compass.cli import app, costs_defaults
from compass.db import connect as compass_connect
from compass.models import InventoryRecord, VerifyReport, ams_today

FIXTURE_DIR = TESTS_DIR / "fixtures" / "compass"

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A temp cwd + COMPASS_DATA_DIR, so the CLI never touches repo data."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.setenv("COMPASS_DATA_DIR", str(ws / "data"))
    return ws


def _invoke(args: list[str], input: str | None = None):
    result = runner.invoke(app, args, input=input)
    assert result.exit_code == 0, (
        f"{args} failed (exit {result.exit_code}):\n{result.output}"
    )
    return result


# ── init ─────────────────────────────────────────────────────────────


def test_init_seeds_placeholder_cost_model(workspace):
    result = _invoke(["init"])

    assert "Database klaar" in result.output
    assert "PLACEHOLDER" in result.output.upper()
    db_path = workspace / "data" / "compass.db"
    assert db_path.is_file()

    conn = compass_connect(db_path)
    rows = conn.execute("SELECT valid_from, note FROM cost_model").fetchall()
    assert len(rows) == 1
    assert rows[0]["valid_from"] == "2020-01-01"
    assert "PLACEHOLDER" in rows[0]["note"]
    conn.close()

    # Init twice must be safe: no second placeholder version.
    result = _invoke(["init"])
    assert "niets geseed" in result.output
    conn = compass_connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM cost_model").fetchone()[0] == 1
    conn.close()


def test_costs_defaults_is_the_contract_placeholder():
    model = costs_defaults()
    assert model.valid_from.isoformat() == "2020-01-01"
    assert "PLACEHOLDER" in model.note
    assert model.cogs_per_unit_cents == 650
    assert model.vat_rate == 0.09
    assert model.payment_fees["ideal"].fixed_cents == 29


# ── demo ─────────────────────────────────────────────────────────────


def test_demo_builds_demo_db_and_fires_reorder_signal(workspace):
    result = _invoke(["demo"])

    assert "Demo-database klaar" in result.output
    assert "compass web --demo" in result.output
    assert "http://127.0.0.1:8010" in result.output

    demo_path = workspace / "data" / "compass-demo.db"
    assert demo_path.is_file()
    conn = compass_connect(demo_path)
    types = {row["type"] for row in conn.execute("SELECT type FROM signals")}
    assert "reorder" in types
    assert "scale_up" not in types  # suppressed by the inventory guard
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] >= 88
    conn.close()

    # Rebuilding must be deterministic, not append to the old database.
    _invoke(["demo"])
    conn = compass_connect(demo_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM signals WHERE type = 'reorder'"
    ).fetchone()[0] == 1
    conn.close()


# ── collect / backfill ───────────────────────────────────────────────


def test_collect_without_cost_model_exits_2(workspace):
    result = runner.invoke(app, ["collect"])
    assert result.exit_code == 2
    assert "Geen kostenmodel" in result.output


def test_collect_with_nothing_configured_exits_2(workspace):
    _invoke(["init"])
    result = runner.invoke(app, ["collect"])
    assert result.exit_code == 2
    assert result.output.count("⏭") == 3  # shopify, meta, bol all skipped
    assert "geen credentials" in result.output
    assert "Geen enkele bron geconfigureerd" in result.output
    assert "compass verify" in result.output
    assert "compass import" in result.output


def test_backfill_with_injected_fixture_sources(workspace, monkeypatch):
    from compass import collector
    from compass.sources.fixture import (
        FixtureBolOrders,
        FixtureInventory,
        FixtureMetaSpend,
        FixtureShopifyOrders,
    )

    _invoke(["init"])

    def fake_build_sources(settings):
        inventory = FixtureInventory(
            [
                InventoryRecord(
                    day=ams_today() - timedelta(days=1), units=500, source="fixture"
                )
            ]
        )
        return (
            [
                FixtureShopifyOrders.from_dir(FIXTURE_DIR),
                FixtureBolOrders.from_dir(FIXTURE_DIR),
            ],
            [FixtureMetaSpend.from_dir(FIXTURE_DIR)],
            [inventory],
            [],
        )

    monkeypatch.setattr(collector, "build_sources", fake_build_sources)
    result = _invoke(["backfill", "--days", "400"])

    assert "✓" in result.output
    assert "fixture:shopify" in result.output
    assert "fixture:meta" in result.output

    conn = compass_connect(workspace / "data" / "compass.db")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM ad_spend_daily").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] > 0
    run = conn.execute("SELECT kind, ok FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["kind"] == "backfill" and run["ok"] == 1
    conn.close()


# ── costs ────────────────────────────────────────────────────────────


def test_costs_show_displays_model_and_break_even(workspace):
    _invoke(["init"])
    result = _invoke(["costs", "show"])
    assert "Huidig kostenmodel" in result.output
    assert "PLACEHOLDER" in result.output
    assert "Break-even ROAS" in result.output
    assert "boekhouder" in result.output  # the fixed VAT note

    # `compass costs` without a subcommand shows the same view.
    bare = _invoke(["costs"])
    assert "Huidig kostenmodel" in bare.output


def test_costs_set_non_interactive_saves_new_version(workspace):
    _invoke(["init"])
    # cogs, then Enter-for-default on the other prompts, empty note, confirm.
    answers = ["7,25", "", "", "", "", "", "", "", "", "eigen kosten", "y"]
    result = _invoke(
        ["costs", "set", "--vanaf", "2026-06-01"], input="\n".join(answers) + "\n"
    )

    assert "Kostenmodel opgeslagen" in result.output
    assert "Nieuwe break-even ROAS" in result.output

    conn = compass_connect(workspace / "data" / "compass.db")
    rows = conn.execute(
        "SELECT valid_from, cogs_per_unit_cents, note FROM cost_model "
        "ORDER BY valid_from"
    ).fetchall()
    conn.close()
    assert len(rows) == 2  # placeholder + the new version
    assert rows[1]["valid_from"] == "2026-06-01"
    assert rows[1]["cogs_per_unit_cents"] == 725  # '7,25' parsed as Dutch euros
    assert rows[1]["note"] == "eigen kosten"


def test_costs_set_declined_saves_nothing(workspace):
    _invoke(["init"])
    answers = ["", "", "", "", "", "", "", "", "", "", "n"]
    result = _invoke(["costs", "set"], input="\n".join(answers) + "\n")

    assert "Niets opgeslagen" in result.output
    conn = compass_connect(workspace / "data" / "compass.db")
    assert conn.execute("SELECT COUNT(*) FROM cost_model").fetchone()[0] == 1
    conn.close()


# ── import & export ──────────────────────────────────────────────────


def _write_orders_csv(path: Path, extra_row: str | None = None) -> None:
    """A ;-separated CSV with Dutch decimals, like Dutch Excel saves it."""
    day = (RUN_DAY - timedelta(days=16)).isoformat()  # 2026-06-15
    lines = [
        "extern_id;channel;ordered_at;gross_eur;units;payment_method",
        f"IMP-1;shopify;{day}T14:32:00+02:00;29,95;1;ideal",
        f"IMP-2;bol;{day};59,90;2;",
    ]
    if extra_row is not None:
        lines.append(extra_row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_import_then_export_round_trip(workspace):
    _invoke(["init"])
    _write_orders_csv(workspace / "orders.csv")

    result = _invoke(["import", "orders.csv"])
    assert "Import (orders)" in result.output
    assert "2 regels" in result.output

    result = _invoke(["export", "--csv", "--json"])
    assert "✓ Export:" in result.output

    payload = json.loads(
        (workspace / "export" / "compass-orders.json").read_text(encoding="utf-8")
    )
    assert len(payload) == 2
    assert {record["gross_cents"] for record in payload} == {2995, 5990}

    with (workspace / "export" / "compass-orders.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert {"extern_id", "channel", "gross_cents"} <= set(rows[0])

    # Every export table lands, even the (possibly empty) signals table.
    for table in ("ad_spend_daily", "daily_metrics", "cost_model", "signals"):
        assert (workspace / "export" / f"compass-{table}.csv").is_file()
        assert (workspace / "export" / f"compass-{table}.json").is_file()


def test_import_bad_row_reports_error_and_exits_1(workspace):
    _invoke(["init"])
    day = (RUN_DAY - timedelta(days=16)).isoformat()
    _write_orders_csv(
        workspace / "orders.csv", extra_row=f"IMP-3;shopify;{day};abc;1;ideal"
    )

    result = runner.invoke(app, ["import", "orders.csv"])
    assert result.exit_code == 1
    assert "regel" in result.output  # "regel 4: …"
    conn = compass_connect(workspace / "data" / "compass.db")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    conn.close()


def test_import_missing_file_exits_1(workspace):
    _invoke(["init"])
    result = runner.invoke(app, ["import", "bestaat-niet.csv"])
    assert result.exit_code == 1
    assert "niet gevonden" in result.output


# ── status & web ─────────────────────────────────────────────────────


def test_status_without_credentials_stays_offline(workspace):
    _invoke(["init"])
    result = _invoke(["status"])

    assert result.output.count("geen credentials") == 3
    assert "nog geen runs" in result.output
    assert "PLACEHOLDER" in result.output.upper() or "placeholder" in result.output
    assert "vanaf 2020-01-01" in result.output
    assert "Break-even ROAS" in result.output


def test_web_demo_without_demo_db_exits_1(workspace):
    result = runner.invoke(app, ["web", "--demo"])
    assert result.exit_code == 1
    assert "compass demo" in result.output


# ── verify (fixture path, offline) ───────────────────────────────────


def test_verify_without_credentials_prints_setup_and_fixture_analysis(
    monkeypatch, capsys
):
    monkeypatch.chdir(REPO_ROOT)
    from compass import verify as verify_mod

    code = verify_mod.run()
    out = capsys.readouterr().out

    assert code == 0
    # Numbered setup instructions per source, pointing at the README sections.
    assert "GEEN SHOPIFY-CREDENTIALS" in out
    assert "Shopify custom app aanmaken" in out
    assert "Meta Marketing API koppelen" in out
    assert "bol Retailer API koppelen" in out
    assert "SHOPIFY_ACCESS_TOKEN" in out and "BOL_CLIENT_SECRET" in out
    # The fixture analysis in adscout-verify shape, once per source.
    assert out.count("FIXTURE-MODUS") == 3
    assert "Aantal orders geparsed: 5 van 6" in out
    assert "Aantal campagne-dagen geparsed: 6" in out
    assert "Aantal bol-orders geparsed: 4" in out
    assert "Veld-dekking" in out
    assert "Voorbeeldrecord" in out


def test_verify_single_source_prints_only_that_source(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    from compass import verify as verify_mod

    code = verify_mod.run("bol")
    out = capsys.readouterr().out

    assert code == 0
    assert "bol Retailer API koppelen" in out
    assert "Shopify custom app aanmaken" not in out
    assert "Meta Marketing API koppelen" not in out


def test_verify_unknown_source_exits_1(capsys):
    from compass import verify as verify_mod

    assert verify_mod.run("amazon") == 1
    assert "Onbekende bron" in capsys.readouterr().out


def test_cli_verify_delegates_exit_code(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(app, ["verify", "shopify"])
    assert result.exit_code == 0
    assert "FIXTURE-MODUS" in result.output


# ── verify (live path, stubbed — no network) ─────────────────────────


def test_verify_live_credentials_problem_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_dood")
    from compass import verify as verify_mod
    from compass.sources import shopify as shopify_mod

    monkeypatch.setattr(
        shopify_mod, "check_access", lambda s: (False, shopify_mod.CREDENTIALS_HELP)
    )
    code = verify_mod.run("shopify")
    out = capsys.readouterr().out

    assert code == 2
    assert "Snelle check" in out
    assert "GEEN SHOPIFY-CREDENTIALS" not in out  # live path, not setup help


def test_verify_live_report_lines_and_issue_exit_codes(monkeypatch, capsys):
    monkeypatch.setenv("SHOPIFY_SHOP", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_ok")
    from compass import verify as verify_mod
    from compass.sources import shopify as shopify_mod

    monkeypatch.setattr(
        shopify_mod, "check_access", lambda s: (True, "toegang ok (shop: Demo)")
    )

    class StubShopify:
        def __init__(self, settings):
            self.ok = True

        def verify(self):
            return VerifyReport(
                source="shopify", ok=self.ok, mode="live", lines=["Shop: Demo BV"]
            )

    monkeypatch.setattr(verify_mod, "ShopifySource", StubShopify)
    assert verify_mod.run("shopify") == 0
    assert "Shop: Demo BV" in capsys.readouterr().out

    class BrokenShopify(StubShopify):
        def __init__(self, settings):
            self.ok = False

    monkeypatch.setattr(verify_mod, "ShopifySource", BrokenShopify)
    assert verify_mod.run("shopify") == 1
