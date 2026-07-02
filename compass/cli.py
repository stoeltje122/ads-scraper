"""Compass CLI — `compass --help` for everything.

The daily run (`compass collect`), the cost model, imports/exports and
the dashboard all live here; nothing is ever configured by editing code.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import typer

from compass import __version__, queries, store
from compass.config import Settings, load_settings, setup_logging
from compass.db import open_db
from compass.models import (
    DISCLAIMER,
    CostModel,
    PaymentFee,
    ams_today,
    fmt_eur,
    parse_eur_to_cents,
)

app = typer.Typer(
    name="compass",
    help="Compass — financieel dashboard en schaal-signalen voor Cloudplunge.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
costs_app = typer.Typer(help="Kostenmodel bekijken (`show`) en bijwerken (`set`).")
app.add_typer(costs_app, name="costs")

EXPORT_TABLES = ("orders", "ad_spend_daily", "daily_metrics", "cost_model", "signals")


def _boot(verbose: bool = False) -> tuple[Settings, sqlite3.Connection]:
    settings = load_settings()
    setup_logging(settings, verbose=verbose)
    conn = open_db(settings.db_path)
    return settings, conn


def costs_defaults() -> CostModel:
    """The placeholder cost model `compass init` seeds.

    Ballpark numbers so the dashboard renders from day one; valid_from
    2020-01-01 covers any backfilled history. The note screams PLACEHOLDER
    until the founders enter their real costs.
    """
    return CostModel(
        valid_from=date(2020, 1, 1),
        cogs_per_unit_cents=650,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=0.09,
        fixed_month_cents=40_000,
        lead_time_days=30,
        safety_factor=1.3,
        note="PLACEHOLDER — vul jullie echte kosten in (README §Kostenmodel)",
    )


# ── Dutch display helpers (CLI edge only; money via fmt_eur) ─────────


def _fmt_ratio(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".replace(".", ",")


def _pct_text(fraction: float) -> str:
    """A fraction as Dutch percent digits: 0.124 → '12,4'."""
    return f"{fraction * 100:g}".replace(".", ",")


def _num_text(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _eur_text(cents: int) -> str:
    """Bare Dutch amount for prompt defaults: 650 → '6,50'."""
    return f"{cents // 100},{cents % 100:02d}"


def _break_even_line(conn: sqlite3.Connection, today: date, prefix: str) -> str:
    """Break-even ROAS over the last 30 days of real data (the same
    window the dashboard uses); '—' until there are orders."""
    totals = queries.window_totals(conn, today - timedelta(days=29), today)
    be_roas = totals.break_even_roas
    if be_roas is None:
        return f"{prefix}: — (nog geen omzetdata in de laatste 30 dagen)"
    return f"{prefix} (laatste 30 dagen): {_fmt_ratio(be_roas)}"


@app.callback()
def _version_callback() -> None:
    """Compass — financieel dashboard voor Cloudplunge. Zie compass/README.md."""


# ── Kerncommando's ───────────────────────────────────────────────────


@app.command()
def init() -> None:
    """DB aanmaken, migraties draaien en een placeholder-kostenmodel seeden."""
    settings, conn = _boot()
    typer.echo(f"✓ Database klaar: {settings.db_path}")
    n_models = conn.execute("SELECT COUNT(*) FROM cost_model").fetchone()[0]
    if n_models == 0:
        store.add_cost_model(conn, costs_defaults())
        typer.secho(
            "⚠  Placeholder-kostenmodel geseed — de marges kloppen pas als "
            "jullie de echte kosten invullen:\n"
            "   `compass costs set` of het dashboard (zie compass/README.md, "
            "sectie 'Kostenmodel invullen').",
            fg="yellow",
        )
    else:
        typer.echo(f"✓ Kostenmodel aanwezig ({n_models} versie(s)) — niets geseed.")
    typer.echo("\nVolgende stappen:")
    typer.echo("  1. compass verify       (werken de koppelingen? zo niet: setup-uitleg)")
    typer.echo("  2. compass costs set    (het echte kostenmodel invullen)")
    typer.echo("  3. compass collect      (eerste dagelijkse run)")
    typer.echo("  4. compass backfill     (historie ophalen, tot 365 dagen)")
    typer.echo("  5. compass web          (dashboard bekijken)")
    typer.echo("Eerst spelen zonder credentials? compass demo && compass web --demo")


@app.command()
def demo(
    days: int = typer.Option(90, help="Aantal dagen demo-historie."),
) -> None:
    """Demo-database bouwen: ~90 dagen fictieve maar realistische data."""
    from compass import demo as demo_mod

    settings = load_settings()
    setup_logging(settings)
    # Fresh build every time: the demo is deterministic, leftovers are not.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.demo_db_path}{suffix}").unlink(missing_ok=True)
    conn = open_db(settings.demo_db_path)
    result = demo_mod.load_demo(conn, days=days)
    conn.close()

    typer.echo(f"✓ Demo-database klaar: {settings.demo_db_path}")
    typer.echo(
        f"  {result.orders_upserted} orders, {result.spend_rows_upserted} "
        f"campagne-dagen spend, {len(result.signals_fired)} signa(a)l(en)."
    )
    if result.errors:
        for error in result.errors:
            typer.secho(f"  ✗  {error}", fg="red")
        raise typer.Exit(1)
    typer.echo("\nBekijken:  compass web --demo  → http://127.0.0.1:8010")


def _run_collect(kind: str, days: int, verbose: bool) -> None:
    """Shared body of collect/backfill: guard, run, report, exit code."""
    from compass import collector
    from compass.signals import signal_type_label

    settings, conn = _boot(verbose=verbose)
    today = ams_today()

    if queries.cost_model_for(conn, today) is None:
        typer.secho(collector.NO_COST_MODEL_ERROR, fg="red")
        raise typer.Exit(2)

    result = collector.collect(
        conn, settings, since=today - timedelta(days=days), kind=kind
    )

    typer.echo(f"Run #{result.run_id} — {result.started_at} → {result.finished_at}")
    for source_result in result.sources:
        if source_result.skipped:
            typer.echo(
                f"  ⏭  {source_result.name}: overgeslagen ({source_result.skipped})"
            )
        elif source_result.error:
            typer.secho(f"  ✗  {source_result.name}: {source_result.error}", fg="red")
        else:
            typer.echo(
                f"  ✓  {source_result.name}: {source_result.fetched} opgehaald, "
                f"{source_result.upserted} nieuw"
            )
    for error in result.run_errors:
        typer.secho(f"  ✗  {error}", fg="red")

    if result.signals_fired:
        typer.echo("\nSignalen:")
        for signal in result.signals_fired:
            typer.secho(
                f"  ⚠  {signal_type_label(signal.type)}: {signal.message}",
                fg="yellow",
            )
        typer.echo(f"\n{DISCLAIMER}")

    if result.errors:
        typer.secho(
            f"\n{len(result.errors)} fout(en) — zie logs/compass.log", fg="yellow"
        )
        raise typer.Exit(1)
    if result.sources and all(s.skipped for s in result.sources):
        typer.secho(
            "\nGeen enkele bron geconfigureerd — er is niets opgehaald.\n"
            "Regel credentials via compass/README.md en test met `compass verify`,\n"
            "of importeer historie uit CSV met `compass import`.",
            fg="yellow",
        )
        raise typer.Exit(2)


@app.command()
def collect(
    days: int = typer.Option(3, help="Overlap-dagen (vangt late refunds op)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """De dagelijkse run: orders, ad spend en voorraad ophalen + signalen."""
    _run_collect(kind="collect", days=days, verbose=verbose)


@app.command()
def backfill(
    days: int = typer.Option(365, help="Aantal dagen historie om op te halen."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Historie ophalen: als collect, maar over een lange periode."""
    _run_collect(kind="backfill", days=days, verbose=verbose)


# ── Kostenmodel ──────────────────────────────────────────────────────


def _show_costs(conn: sqlite3.Connection, today: date) -> None:
    model = queries.current_cost_model(conn, today)
    if model is None:
        typer.echo("Nog geen kostenmodel — draai `compass init` of `compass costs set`.")
        return
    typer.echo(f"Huidig kostenmodel (geldig vanaf {model.valid_from.isoformat()}):")
    typer.echo(f"  Inkoopprijs per stuk (COGS):  {fmt_eur(model.cogs_per_unit_cents)}")
    typer.echo(f"  Verzendkosten per order:      {fmt_eur(model.shipping_per_order_cents)}")
    typer.echo(
        f"  Betaalfee standaard:          {_pct_text(model.fee_pct)}% + "
        f"{fmt_eur(model.fee_fixed_cents)} per order"
    )
    for method, fee in sorted(model.payment_fees.items()):
        typer.echo(
            f"  Betaalfee {method + ':':20s}{_pct_text(fee.pct)}% + "
            f"{fmt_eur(fee.fixed_cents)} per order"
        )
    typer.echo(f"  bol-commissie:                {_pct_text(model.bol_commission_pct)}%")
    typer.echo(
        f"  Btw-tarief:                   {_pct_text(model.vat_rate)}% "
        "(laat het btw-tarief bevestigen door jullie boekhouder)"
    )
    typer.echo(f"  Vaste lasten per maand:       {fmt_eur(model.fixed_month_cents)}")
    typer.echo(
        f"  Levertijd voorraad:           {model.lead_time_days} dagen "
        f"(veiligheidsfactor {_num_text(model.safety_factor)})"
    )
    if model.note:
        typer.echo(f"  Notitie:                      {model.note}")
    if model.note and "PLACEHOLDER" in model.note:
        typer.secho(
            "⚠  Dit is nog het placeholder-kostenmodel — vul jullie echte "
            "kosten in met `compass costs set`.",
            fg="yellow",
        )
    typer.echo(_break_even_line(conn, today, "Break-even ROAS"))

    history = queries.cost_model_history(conn)
    if len(history) > 1:
        typer.echo("\nVersiegeschiedenis (nieuwste eerst):")
        for version in history:
            typer.echo(
                f"  vanaf {version.valid_from.isoformat()} — "
                f"COGS {fmt_eur(version.cogs_per_unit_cents)}/stuk"
                + (f" — {version.note}" if version.note else "")
            )


@costs_app.callback(invoke_without_command=True)
def costs_main(ctx: typer.Context) -> None:
    """Zonder subcommando: het huidige kostenmodel tonen."""
    if ctx.invoked_subcommand is None:
        _, conn = _boot()
        _show_costs(conn, ams_today())


@costs_app.command("show")
def costs_show() -> None:
    """Huidig kostenmodel, versiegeschiedenis en break-even ROAS."""
    _, conn = _boot()
    _show_costs(conn, ams_today())


def _prompt_eur(label: str, default_cents: int) -> int:
    while True:
        raw = typer.prompt(f"{label} (euro)", default=_eur_text(default_cents))
        try:
            cents = parse_eur_to_cents(str(raw))
        except ValueError:
            cents = None
        if cents is None or cents < 0:
            typer.echo("Ongeldig bedrag — gebruik bijv. 6,50.")
            continue
        return cents


def _prompt_pct(label: str, default_fraction: float) -> float:
    while True:
        raw = typer.prompt(f"{label} (%)", default=_pct_text(default_fraction))
        try:
            value = float(str(raw).strip().replace("%", "").replace(",", "."))
        except ValueError:
            typer.echo("Ongeldig percentage — gebruik bijv. 12,4.")
            continue
        if not 0 <= value <= 100:
            typer.echo("Ongeldig percentage — moet tussen 0 en 100 liggen.")
            continue
        return value / 100


def _prompt_factor(label: str, default: float) -> float:
    while True:
        raw = typer.prompt(label, default=_num_text(default))
        try:
            value = float(str(raw).strip().replace(",", "."))
        except ValueError:
            typer.echo("Ongeldig getal — gebruik bijv. 1,3.")
            continue
        if value <= 0:
            typer.echo("Moet groter dan 0 zijn.")
            continue
        return value


@costs_app.command("set")
def costs_set(
    vanaf: str = typer.Option(
        None, "--vanaf", help="Ingangsdatum JJJJ-MM-DD (default: vandaag)."
    ),
) -> None:
    """Nieuwe kostenmodel-versie invullen (interactief, Nederlandse notatie)."""
    _, conn = _boot()
    today = ams_today()
    try:
        valid_from = date.fromisoformat(vanaf) if vanaf else today
    except ValueError:
        typer.secho(f"Ongeldige datum {vanaf!r} — gebruik JJJJ-MM-DD.", fg="red")
        raise typer.Exit(1)

    current = queries.current_cost_model(conn, today) or costs_defaults()
    typer.echo(
        "Nieuwe kostenmodel-versie. Bedragen op z'n Nederlands (bijv. 6,50), "
        "percentages als procenten (bijv. 12,4). Enter = huidige waarde."
    )
    cogs = _prompt_eur("Inkoopprijs per stuk (COGS, incl. inbound)", current.cogs_per_unit_cents)
    shipping = _prompt_eur("Verzendkosten per order", current.shipping_per_order_cents)
    fee_pct = _prompt_pct("Betaalfee standaard", current.fee_pct)
    fee_fixed = _prompt_eur("Betaalfee vast per order", current.fee_fixed_cents)
    bol_pct = _prompt_pct("bol-commissie", current.bol_commission_pct)
    vat = _prompt_pct("Btw-tarief (laat bevestigen door jullie boekhouder)", current.vat_rate)
    fixed = _prompt_eur("Vaste lasten per maand", current.fixed_month_cents)
    lead = typer.prompt(
        "Levertijd nieuwe voorraad (dagen)", default=current.lead_time_days, type=int
    )
    safety = _prompt_factor("Veiligheidsfactor voorraad", current.safety_factor)
    note = typer.prompt(
        "Notitie (bijv. 'nieuwe leverancier')", default="", show_default=False
    ).strip()

    model = CostModel(
        valid_from=valid_from,
        cogs_per_unit_cents=cogs,
        shipping_per_order_cents=shipping,
        fee_pct=fee_pct,
        fee_fixed_cents=fee_fixed,
        # Per-method fees keep their current values; fine-tuning per
        # betaalmethode happens in the dashboard (Instellingen → Kostenmodel).
        payment_fees=dict(current.payment_fees),
        bol_commission_pct=bol_pct,
        vat_rate=vat,
        fixed_month_cents=fixed,
        lead_time_days=lead,
        safety_factor=safety,
        note=note or None,
    )

    typer.echo(f"\nSamenvatting (geldig vanaf {valid_from.isoformat()}):")
    typer.echo(f"  COGS {fmt_eur(cogs)}/stuk, verzending {fmt_eur(shipping)}/order")
    typer.echo(
        f"  Betaalfee {_pct_text(fee_pct)}% + {fmt_eur(fee_fixed)}; "
        f"bol-commissie {_pct_text(bol_pct)}%; btw {_pct_text(vat)}%"
    )
    if model.payment_fees:
        methods = ", ".join(
            f"{method} {_pct_text(fee.pct)}% + {fmt_eur(fee.fixed_cents)}"
            for method, fee in sorted(model.payment_fees.items())
        )
        typer.echo(f"  Betaalfees per methode (ongewijzigd): {methods}")
    typer.echo(
        f"  Vaste lasten {fmt_eur(fixed)}/maand; levertijd {lead} dagen "
        f"× factor {_num_text(safety)}"
    )
    if not typer.confirm("Opslaan?"):
        typer.echo("Niets opgeslagen.")
        return

    store.add_cost_model(conn, model)
    bounds = queries.data_bounds(conn)
    if bounds is not None:
        store.rebuild_daily_metrics(conn, bounds[0], max(bounds[1], today))
        typer.echo("✓ Dagcijfers herbouwd met het nieuwe kostenmodel.")
    typer.echo(f"✓ Kostenmodel opgeslagen (geldig vanaf {valid_from.isoformat()}).")
    typer.echo(_break_even_line(conn, today, "Nieuwe break-even ROAS"))


# ── Rapport, status, verify ──────────────────────────────────────────


@app.command()
def report(
    weekly: bool = typer.Option(
        True, "--weekly", help="Weekrapport (voorlopig het enige rapporttype)."
    ),
) -> None:
    """Weekrapport genereren (Markdown + HTML in reports/)."""
    from compass import report as report_mod
    from compass.notify import get_notifier

    settings, conn = _boot()
    md_path, html_path = report_mod.write_weekly(conn, settings.reports_dir)
    typer.echo(f"✓ Rapport geschreven:\n  {md_path}\n  {html_path}")
    get_notifier().send_report(
        f"Compass weekrapport {ams_today().isoformat()}",
        md_path.read_text(encoding="utf-8"),
        [md_path, html_path],
    )


@app.command()
def status() -> None:
    """Database, bronnen, laatste runs, databereik en signalen. Gaat alleen
    het netwerk op voor bronnen waarvan credentials aanwezig zijn."""
    settings, conn = _boot()
    today = ams_today()

    size_kb = settings.db_path.stat().st_size / 1024
    typer.echo(f"Compass v{__version__} — database: {settings.db_path} ({size_kb:.0f} kB)")

    typer.echo("\nBronnen:")
    if settings.has_shopify():
        from compass.sources.shopify import check_access

        ok, message = check_access(settings)
        typer.secho(f"  Shopify: {message}", fg="green" if ok else "red")
    else:
        typer.echo(
            "  Shopify: geen credentials (zie compass/README.md, "
            "sectie 'Shopify custom app aanmaken')"
        )
    if settings.has_meta():
        from compass.sources.meta_insights import check_token

        ok, message = check_token(settings)
        typer.secho(f"  Meta: {message}", fg="green" if ok else "red")
    else:
        typer.echo(
            "  Meta: geen credentials (zie compass/README.md, "
            "sectie 'Meta Marketing API koppelen')"
        )
    if settings.has_bol():
        typer.echo("  bol: credentials aanwezig (testen: `compass verify bol`)")
    else:
        typer.echo(
            "  bol: geen credentials (zie compass/README.md, "
            "sectie 'bol Retailer API koppelen')"
        )

    typer.echo("\nLaatste runs:")
    runs = queries.last_runs(conn, limit=5)
    if not runs:
        typer.echo("  (nog geen runs — draai `compass collect` of `compass demo`)")
    for run in runs:
        state = "OK" if run["ok"] else "MET FOUTEN"
        typer.echo(
            f"  #{run['id']} {run['started_at']} [{run['kind']}]: {state} — "
            f"{run['orders_upserted']} orders nieuw, "
            f"{run['spend_rows_upserted']} spend-regels nieuw"
        )
        for error in json.loads(run["errors"] or "[]"):
            typer.secho(f"      fout: {error}", fg="red")

    bounds = queries.data_bounds(conn)
    if bounds is None:
        typer.echo(
            "\nDatabereik: nog geen orders of spend — "
            "`compass collect`, `compass import` of `compass demo`."
        )
    else:
        typer.echo(f"\nDatabereik: {bounds[0].isoformat()} t/m {bounds[1].isoformat()}")

    n_active = len(queries.active_signals(conn))
    typer.echo(f"Actieve signalen: {n_active}" + (" (zie dashboard)" if n_active else ""))

    model = queries.current_cost_model(conn, today)
    if model is None:
        typer.secho(
            "Kostenmodel: ontbreekt — draai `compass init` of `compass costs set`.",
            fg="red",
        )
    else:
        typer.echo(
            f"Kostenmodel: vanaf {model.valid_from.isoformat()} — "
            f"COGS {fmt_eur(model.cogs_per_unit_cents)}/stuk, "
            f"vaste lasten {fmt_eur(model.fixed_month_cents)}/maand"
        )
        if model.note and "PLACEHOLDER" in model.note:
            typer.secho(
                "  ⚠  Nog het placeholder-kostenmodel — vul jullie echte kosten "
                "in met `compass costs set`.",
                fg="yellow",
            )
        typer.echo(_break_even_line(conn, today, "Break-even ROAS"))


@app.command()
def verify(
    bron: str = typer.Argument(
        None, help="shopify, meta of bol (leeg = alle drie)."
    ),
) -> None:
    """Fase 0: verifieer per bron wat de API echt teruggeeft (of fixture-demo)."""
    from compass import verify as verify_mod

    raise typer.Exit(verify_mod.run(bron))


# ── Import & export ──────────────────────────────────────────────────


@app.command("import")
def import_(
    path: Path = typer.Argument(..., help="CSV-bestand (orders, spend of inventory)."),
    type_: str = typer.Option(
        "auto", "--type", help="orders, spend, inventory of auto (herkennen)."
    ),
) -> None:
    """CSV importeren: historie, kanalen zonder API, handmatige tellingen.
    Kolomformaten: zie compass/README.md, sectie CSV-import."""
    from compass import importer

    _, conn = _boot()
    if not path.is_file():
        typer.secho(f"Bestand niet gevonden: {path}", fg="red")
        raise typer.Exit(1)
    try:
        result = importer.import_file(conn, path, kind=None if type_ == "auto" else type_)
    except ValueError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)

    typer.echo(
        f"✓ Import ({result.kind}): {result.rows} regels gelezen, "
        f"{result.upserted} nieuw."
    )
    for error in result.errors:
        typer.secho(f"  ✗  {error}", fg="red")
    if result.errors:
        typer.secho(
            "Sommige regels zijn overgeslagen — corrigeer ze en importeer "
            "opnieuw (dubbel importeren is veilig).",
            fg="yellow",
        )
        raise typer.Exit(1)


@app.command()
def export(
    csv_out: bool = typer.Option(False, "--csv", help="Exporteer naar CSV."),
    json_out: bool = typer.Option(False, "--json", help="Exporteer naar JSON."),
    out: Path = typer.Option(Path("export"), "--out", help="Uitvoermap."),
) -> None:
    """Volledige data-export — geen lock-in, handig voor eigen analyses."""
    if not csv_out and not json_out:
        csv_out = True
    _, conn = _boot()
    out.mkdir(parents=True, exist_ok=True)

    for table in EXPORT_TABLES:
        cursor = conn.execute(f"SELECT * FROM {table}")  # fixed table names
        columns = [description[0] for description in cursor.description]
        records = [dict(zip(columns, row)) for row in cursor.fetchall()]
        if csv_out:
            path = out / f"compass-{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(records)
            typer.echo(f"✓ Export: {path} ({len(records)} rijen)")
        if json_out:
            path = out / f"compass-{table}.json"
            path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            typer.echo(f"✓ Export: {path} ({len(records)} rijen)")


# ── Dashboard ────────────────────────────────────────────────────────


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind-adres (localhost)."),
    port: int = typer.Option(
        8010, help="Poort (8010, zodat AdScout op 8000 tegelijk kan draaien)."
    ),
    demo: bool = typer.Option(False, "--demo", help="Dashboard op de demo-database."),
) -> None:
    """Dashboard starten op http://localhost:8010."""
    settings = load_settings()
    setup_logging(settings)
    if demo and not settings.demo_db_path.exists():
        typer.secho("Nog geen demo-database — draai eerst `compass demo`.", fg="red")
        raise typer.Exit(1)

    import uvicorn

    from compass.web.app import create_app

    # Validate/migrate the database before uvicorn takes over the process.
    conn = open_db(settings.demo_db_path if demo else settings.db_path)
    conn.close()
    typer.echo(f"Compass dashboard: http://{host}:{port}" + (" (demo)" if demo else ""))
    uvicorn.run(create_app(settings, demo=demo), host=host, port=port, log_level="info")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
