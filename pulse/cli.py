"""Pulse CLI — `pulse --help` voor alles.

Bronnen, concurrenten en thema's beheer je hier of in het dashboard;
nooit door code te wijzigen.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import typer

from pulse import __version__, analysis, queries, store
from pulse.config import Settings, load_settings, setup_logging
from pulse.db import open_db

app = typer.Typer(
    name="pulse",
    help="Pulse — feedback- en luistertool voor Cloudplunge.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
source_app = typer.Typer(help="Bronnen beheren (aan/uit/status).", no_args_is_help=True)
competitor_app = typer.Typer(help="Concurrenten-watchlist beheren.", no_args_is_help=True)
theme_app = typer.Typer(help="Thema-taxonomie beheren.", no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(competitor_app, name="competitor")
app.add_typer(theme_app, name="theme")

DEFAULT_FIXTURE_DIR = Path("tests/fixtures/pulse")
SOURCE_TYPES = [t for t, *_ in store.STANDARD_SOURCES]


def _boot(verbose: bool = False) -> tuple[Settings, sqlite3.Connection]:
    settings = load_settings()
    setup_logging(settings, verbose=verbose)
    conn = open_db(settings.db_path)
    return settings, conn


def _require_init(conn: sqlite3.Connection, settings: Settings) -> None:
    """Guard against a silently-wrong environment (verkeerde map / init vergeten)."""
    n = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    if n == 0:
        typer.secho(
            f"Lege database in {settings.db_path.resolve()} — is dit de juiste map?\n"
            "Draai `pulse init` in de projectmap, of zet PULSE_DATA_DIR in .env.",
            fg="red",
        )
        raise typer.Exit(2)


# ── Kerncommando's ───────────────────────────────────────────────────


@app.command()
def init() -> None:
    """DB aanmaken, migraties draaien en bronnen + thema's + watchlist seeden."""
    settings, conn = _boot()
    n_src = store.seed_sources(conn)
    n_themes = n_comp = 0
    if Path("pulse-taxonomy.seed.yaml").exists():
        n_themes = store.seed_themes(conn, "pulse-taxonomy.seed.yaml")
    else:
        typer.secho(
            "⚠ pulse-taxonomy.seed.yaml niet gevonden — sta je wel in de projectmap "
            "(ads-scraper)? Zonder thema's kan de AI-analyse niet draaien.",
            fg="yellow",
        )
    if Path("competitors.seed.yaml").exists():
        n_comp = store.seed_competitors(conn, "competitors.seed.yaml")
    else:
        typer.secho("⚠ competitors.seed.yaml niet gevonden — watchlist blijft leeg.", fg="yellow")
    typer.echo(f"✓ Database klaar: {settings.db_path}")
    typer.echo(f"✓ Bronnen geseed: {n_src} nieuw (van {len(store.STANDARD_SOURCES)})")
    typer.echo(f"✓ Thema's geseed: {n_themes}")
    typer.echo(f"✓ Watchlist geseed: {n_comp} concurrenten (gedeeld met AdScout)")
    typer.echo("\nVolgende stappen:")
    typer.echo("  1. pulse demo        (dashboard vullen met voorbeelddata — niets nodig)")
    typer.echo("  2. pulse verify      (checken welke echte bronnen al werken)")
    typer.echo("  3. pulse web         (dashboard bekijken)")


@app.command()
def demo() -> None:
    """Alles op voorbeelddata: init + verzamelen + analyse + weekrapport.

    Draait volledig offline, geen sleutels nodig. Daarna: pulse web.
    """
    from pulse import collector, report as report_mod

    settings, conn = _boot()
    store.seed_sources(conn)
    if Path("pulse-taxonomy.seed.yaml").exists():
        store.seed_themes(conn, "pulse-taxonomy.seed.yaml")
    if Path("competitors.seed.yaml").exists():
        store.seed_competitors(conn, "competitors.seed.yaml")
    if not DEFAULT_FIXTURE_DIR.exists():
        typer.secho(
            f"Fixture-map niet gevonden: {DEFAULT_FIXTURE_DIR} — draai dit commando "
            "vanuit de projectmap (ads-scraper).",
            fg="red",
        )
        raise typer.Exit(2)

    result = collector.collect(conn, settings, fixture_dir=DEFAULT_FIXTURE_DIR)
    typer.echo(f"✓ Voorbeelddata verzameld: {result.items_new} items")

    # Manual-import fixtures (competitor reviews) go through the same import
    # path the founders will use.
    from pulse.sources import manual

    csv_path = DEFAULT_FIXTURE_DIR / "competitor_reviews.csv"
    if csv_path.exists():
        imported = collector.import_items(conn, manual.parse_file(csv_path))
        typer.echo(f"✓ Concurrent-reviews geïmporteerd: {imported.items_new} items")

    outcome = analysis.analyze_pending(
        conn, settings, analyzer=analysis.CannedAnalyzer(DEFAULT_FIXTURE_DIR)
    )
    typer.echo(f"✓ Analyse (demo, zonder API): {outcome['analyzed']} items")

    md_path, _ = report_mod.write_weekly(conn, settings.reports_dir)
    typer.echo(f"✓ Weekrapport: {md_path}")
    typer.echo("\nKlaar! Bekijk het dashboard:  pulse web")


@app.command()
def sources() -> None:
    """Alle bronnen met status, aantallen en laatste run."""
    settings, conn = _boot()
    _require_init(conn, settings)
    for src in store.list_sources(conn):
        label = store.STATUS_LABELS_NL.get(src["status"], src["status"])
        color = {"active": "green", "awaiting_config": "yellow", "paused": None}.get(src["status"])
        queue = f", {src['n_unanalyzed'] or 0} wachten op analyse" if src["n_unanalyzed"] else ""
        last = f" — laatste run {src['last_run']}" if src["last_run"] else ""
        typer.secho(
            f"  [{src['id']}] {src['name']} ({src['type']}): {label} — "
            f"{src['n_items']} items{queue}{last}",
            fg=color,
        )
        if src["config_json"]:
            note = json.loads(src["config_json"]).get("reden")
            if note:
                typer.echo(f"        ↳ {note}")


@app.command()
def collect(
    fixtures: bool = typer.Option(False, "--fixtures", help="Voorbeelddata i.p.v. echte API's (offline testen)."),
    fixture_dir: Path = typer.Option(DEFAULT_FIXTURE_DIR, help="Map met fixture-bestanden."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """De dagelijkse run: alle actieve bronnen ophalen + retentie-opschoning."""
    from pulse import collector

    settings, conn = _boot(verbose=verbose)
    _require_init(conn, settings)
    result = collector.collect(conn, settings, fixture_dir=fixture_dir if fixtures else None)

    typer.echo(f"\nRun #{result.run_id} — {result.started_at} → {result.finished_at}")
    for r in result.per_source:
        if r.skipped:
            typer.echo(f"  ⏭  {r.source}: overgeslagen ({r.skipped})")
        elif r.error:
            typer.secho(f"  ✗  {r.source}: {r.error}", fg="red")
        elif r.warning:
            typer.secho(f"  ⚠  {r.source}: {r.warning}", fg="yellow")
        else:
            typer.echo(f"  ✓  {r.source}: {r.items_seen} items, {r.items_new} nieuw")
    if result.items_deleted:
        typer.echo(f"  🧹 Retentie: {result.items_deleted} oude items opgeschoond")
    typer.echo(f"\nTotaal: {result.items_seen} items, {result.items_new} nieuw")

    n_queue = analysis.queue_size(conn)
    if n_queue:
        if load_settings().anthropic_api_key:
            typer.echo(f"{n_queue} items wachten op analyse — draai: pulse analyze")
        else:
            typer.echo(
                f"{n_queue} items wachten op analyse. Er is nog geen ANTHROPIC_API_KEY "
                "ingesteld — geen probleem, verzamelen gaat gewoon door. "
                "Zie PULSE.md om analyse aan te zetten."
            )
    if result.errors:
        typer.secho(f"{len(result.errors)} bron(nen) faalde(n) — zie logs/pulse.log", fg="yellow")
        raise typer.Exit(1)


@app.command()
def analyze(
    limit: int = typer.Option(200, help="Max aantal items per run (kostenbewust)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """AI-analyse van de wachtrij (vereist ANTHROPIC_API_KEY in .env)."""
    settings, conn = _boot(verbose=verbose)
    _require_init(conn, settings)
    try:
        outcome = analysis.analyze_pending(conn, settings, limit=limit)
    except analysis.AnalyzerUnavailable as exc:
        typer.secho(str(exc), fg="yellow")
        raise typer.Exit(1)
    typer.echo(
        f"✓ {outcome['analyzed']} items geanalyseerd, {outcome['failed']} mislukt "
        f"(heranalyse volgt), {outcome['remaining']} nog in wachtrij."
    )
    n_urgent = queries.count_urgent_open(conn)
    if n_urgent:
        typer.secho(
            f"⚠ {n_urgent} openstaande urgente melding(en) — bekijk ze: pulse web",
            fg="red",
        )


@app.command("import")
def import_(
    file: Path = typer.Argument(..., help="CSV- of tekstbestand (zie PULSE.md voor het formaat)."),
) -> None:
    """Handmatige import: reviews/comments uit een bestand, mét dedupe."""
    from pulse import collector
    from pulse.sources import manual
    from pulse.sources.base import SourceError

    settings, conn = _boot()
    _require_init(conn, settings)
    try:
        items = manual.parse_file(file)
    except SourceError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)
    result = collector.import_items(conn, items)
    dupes = result.items_seen - result.items_new
    typer.echo(f"✓ {result.items_new} items geïmporteerd" +
               (f" ({dupes} al bekend, overgeslagen)" if dupes else ""))
    typer.echo("De items staan in de analyse-wachtrij — draai: pulse analyze")


@app.command()
def report(
    weekly: bool = typer.Option(True, "--weekly", help="Weekrapport (voorlopig het enige rapporttype)."),
) -> None:
    """Weekrapport genereren (Markdown + HTML in reports/)."""
    from datetime import date

    from pulse import report as report_mod
    from pulse.notify import get_notifier

    settings, conn = _boot()
    _require_init(conn, settings)
    md_path, html_path = report_mod.write_weekly(conn, settings.reports_dir)
    typer.echo(f"✓ Rapport geschreven:\n  {md_path}\n  {html_path}")
    get_notifier().send_report(
        f"Pulse weekrapport {date.today().isoformat()}",
        md_path.read_text(encoding="utf-8"),
        [md_path, html_path],
    )


@app.command()
def status() -> None:
    """Bronnen, wachtrij, credential-gezondheid en laatste runs."""
    settings, conn = _boot()
    _require_init(conn, settings)
    typer.echo(f"Pulse v{__version__} — database: {settings.db_path}")

    summary = queries.counts_summary(conn)
    typer.echo(
        f"\nItems: {summary['items']} totaal, {summary['analyzed']} geanalyseerd, "
        f"{summary['queue']} in wachtrij"
    )
    if summary["urgent_open"]:
        typer.secho(f"⚠ {summary['urgent_open']} openstaande urgente melding(en)!", fg="red")
    if summary["failed"]:
        typer.secho(
            f"⚠ {summary['failed']} item(s) wachten op heranalyse (parse-fout) — "
            "draai `pulse analyze` opnieuw.",
            fg="yellow",
        )
    if summary["queue"] and not settings.anthropic_api_key:
        typer.echo(
            "Er is nog geen ANTHROPIC_API_KEY ingesteld — items blijven netjes in de "
            "wachtrij. Zie PULSE.md, sectie 'AI-analyse aanzetten'."
        )

    typer.echo("\nBronnen (credential-gezondheid zonder netwerk; echte test: pulse verify):")
    from pulse.sources import build_adapter
    from pulse.sources.gmail import GmailAdapter

    for src in store.list_sources(conn):
        label = store.STATUS_LABELS_NL.get(src["status"], src["status"])
        last = src["last_run"] or "nooit"
        adapter = build_adapter(src["type"], settings)
        if src["type"] == "gmail":
            gmail: GmailAdapter = adapter
            cred = (
                "gekoppeld" if gmail.authorized
                else "credentials aanwezig, nog niet ingelogd (pulse verify gmail)"
                if gmail.configured else "geen credentials"
            )
        elif src["type"] == "meta_comments":
            cred = "token ingesteld" if adapter.configured else "geen token in .env"
        elif src["type"] == "manual":
            cred = "geen configuratie nodig"
        else:
            cred = "handmatige route (zie PULSE.md)"
        typer.echo(
            f"  {src['name']}: {label} — {cred} — {src['n_items']} items — laatste run: {last}"
        )

    typer.echo("\nLaatste runs:")
    runs = queries.last_runs(conn, limit=5)
    if not runs:
        typer.echo("  (nog geen runs — draai `pulse collect` of `pulse demo`)")
    for run in runs:
        state = "OK" if run["ok"] else "MET FOUTEN"
        typer.echo(
            f"  #{run['id']} {run['kind']} {run['started_at']}: {state} — "
            f"{run['items_seen']} gezien, {run['items_new']} nieuw, "
            f"{run['items_analyzed']} geanalyseerd"
        )
        for err in json.loads(run["errors"] or "[]"):
            typer.secho(f"      fout: {err}", fg="red")


@app.command()
def export(
    csv_out: bool = typer.Option(False, "--csv", help="Exporteer naar CSV."),
    json_out: bool = typer.Option(False, "--json", help="Exporteer naar JSON."),
    out: Path = typer.Option(None, help="Uitvoerpad (default: exports/pulse-<datum>.<ext>)."),
) -> None:
    """Volledige data-export — geen lock-in, handig voor eigen analyses."""
    if not csv_out and not json_out:
        csv_out = True
    if out and csv_out and json_out:
        typer.secho("--out kan alleen met één formaat tegelijk (--csv óf --json).", fg="red")
        raise typer.Exit(1)
    settings, conn = _boot()
    _require_init(conn, settings)
    rows = queries.export_rows(conn)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    def flat(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "bron": row["source_name"],
            "over": row["competitor_name"] or "Cloudplunge",
            "datum": row["happened_at"],
            "auteur": row["author_display"],
            "auteur_hash": row["author_hash"],
            "tekst": row["text"],
            "url": row["url"],
            "taal": row["language"],
            "sentiment": row["sentiment"],
            "themas": "; ".join(json.loads(row["themes_json"] or "[]")) if row["themes_json"] else "",
            "urgentie": row["urgency"],
            "type": row["item_type"],
            "gezondheidsflag": row["health_flag"],
            "concurrent_pluspunt": row["competitor_pro"],
            "concurrent_klacht": row["competitor_con"],
            "opgevolgd_op": row["followed_up_at"],
            "eerste_keer_gezien": row["first_seen"],
        }

    written: list[Path] = []
    if csv_out:
        path = out if (out and not json_out) else Path("exports") / f"pulse-{stamp}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = None
            for row in rows:
                record = flat(row)
                if writer is None:
                    writer = csv.DictWriter(fh, fieldnames=list(record))
                    writer.writeheader()
                writer.writerow(record)
            if writer is None:
                fh.write("")
        written.append(path)
    if json_out:
        path = out if (out and not csv_out) else Path("exports") / f"pulse-{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for row in rows:
            record = flat(row)
            record["themas"] = json.loads(row["themes_json"] or "[]") if row["themes_json"] else []
            payload.append(record)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    for path in written:
        typer.echo(f"✓ Export: {path} ({len(rows)} items)")


@app.command()
def forget(
    who: str = typer.Argument(..., help="Auteur-hash (zie dashboard) of e-mailadres."),
    yes: bool = typer.Option(False, "--yes", help="Zonder bevestigingsvraag (voor scripts)."),
) -> None:
    """AVG: verwijder álle items van één persoon, definitief."""
    settings, conn = _boot()
    _require_init(conn, settings)
    from pulse.models import author_hash

    hashes = {who.strip().casefold(), author_hash(who)}
    n = conn.execute(
        f"SELECT COUNT(*) FROM items WHERE author_hash IN ({','.join('?' for _ in hashes)})",
        tuple(hashes),
    ).fetchone()[0]
    if n == 0:
        typer.echo("Geen items gevonden voor deze persoon — niets te verwijderen.")
        return
    if not yes and not typer.confirm(
        f"Definitief {n} item(s) van deze persoon verwijderen (incl. analyses)?"
    ):
        typer.echo("Geannuleerd.")
        raise typer.Exit(1)
    deleted = store.forget(conn, who)
    typer.echo(f"✓ {deleted} item(s) verwijderd. Deze persoon is vergeten.")


@app.command()
def verify(
    source: str = typer.Argument(None, help=f"Eén bron ({', '.join(SOURCE_TYPES)}) of leeg voor alle."),
    fixtures: bool = typer.Option(False, "--fixtures", help="Check de fixture-bestanden i.p.v. echte credentials."),
    fixture_dir: Path = typer.Option(DEFAULT_FIXTURE_DIR, help="Map met fixture-bestanden."),
) -> None:
    """Per bron één minimale echte call: wat werkt al, wat nog niet?

    Voor Gmail start dit de eenmalige browser-login zodra credentials.json
    er staat (zie PULSE.md).
    """
    from pulse.sources import build_adapter
    from pulse.sources.base import CredentialsError

    settings, conn = _boot()
    types = [source] if source else SOURCE_TYPES
    any_fail = False
    for type_ in types:
        if type_ not in SOURCE_TYPES:
            typer.secho(f"Onbekende bron: {type_} (kies uit: {', '.join(SOURCE_TYPES)})", fg="red")
            raise typer.Exit(2)
        adapter = build_adapter(type_, settings, fixture_dir=fixture_dir if fixtures else None)
        try:
            result = adapter.verify()
        except CredentialsError as exc:
            result_ok, message, detail = False, str(exc), []
        else:
            result_ok, message, detail = result.ok, result.message, result.detail
        glyph = "✓" if result_ok else "✗"
        typer.secho(f"{glyph} {type_}: {message}", fg="green" if result_ok else "red")
        for line in detail:
            typer.echo(f"    {line}")
        any_fail = any_fail or not result_ok
    if any_fail and not fixtures:
        typer.echo("\nNiet alles werkt al — dat is oké: bronnen zonder credentials "
                   "blijven netjes wachten. Zie PULSE.md voor de setup per bron.")
        raise typer.Exit(1)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind-adres (localhost)."),
    port: int = typer.Option(8010, help="Poort (AdScout gebruikt 8000)."),
) -> None:
    """Dashboard starten op http://localhost:8010."""
    import uvicorn

    from pulse.web.app import create_app

    settings, conn = _boot()
    _require_init(conn, settings)
    conn.close()  # web app opens its own connections
    typer.echo(f"Pulse dashboard: http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


# ── Bronnen ──────────────────────────────────────────────────────────


def _require_source(conn: sqlite3.Connection, type_: str) -> sqlite3.Row:
    src = store.get_source(conn, type_)
    if not src:
        typer.secho(f"Bron '{type_}' niet gevonden. Zie: pulse sources", fg="red")
        raise typer.Exit(1)
    return src


@source_app.command("activate")
def source_activate(type_: str = typer.Argument(..., metavar="BRON")) -> None:
    """Bron aanzetten (na een geslaagde `pulse verify <bron>`)."""
    _, conn = _boot()
    src = _require_source(conn, type_)
    store.set_source_status(conn, src["id"], "active")
    typer.echo(f"✓ {src['name']} staat aan. Volgende `pulse collect` neemt hem mee.")


@source_app.command("pause")
def source_pause(type_: str = typer.Argument(..., metavar="BRON")) -> None:
    """Pauzeren: data blijft staan, wordt niet meer opgehaald."""
    _, conn = _boot()
    src = _require_source(conn, type_)
    store.set_source_status(conn, src["id"], "paused")
    typer.echo(f"✓ {src['name']} gepauzeerd.")


@source_app.command("resume")
def source_resume(type_: str = typer.Argument(..., metavar="BRON")) -> None:
    _, conn = _boot()
    src = _require_source(conn, type_)
    store.set_source_status(conn, src["id"], "active")
    typer.echo(f"✓ {src['name']} weer actief.")


# ── Concurrenten ─────────────────────────────────────────────────────


def _require_competitor(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    comp = store.get_competitor(conn, name)
    if not comp:
        typer.secho(f'Concurrent "{name}" niet gevonden. Zie: pulse competitor list', fg="red")
        raise typer.Exit(1)
    return comp


@competitor_app.command("add")
def competitor_add(
    name: str,
    category: str = typer.Option(None, help="Categorie (bijv. supplement, sleep-comfort)."),
    notes: str = typer.Option(None),
) -> None:
    _, conn = _boot()
    store.add_competitor(conn, name, category, notes)
    typer.echo(f'✓ Concurrent "{name}" toegevoegd. Review-URL\'s instellen: '
               f'pulse competitor set-urls "{name}" <url> [<url> ...]')


@competitor_app.command("list")
def competitor_list() -> None:
    _, conn = _boot()
    for comp in store.list_competitors(conn):
        urls = json.loads(comp["review_urls_json"] or "[]")
        flag = "" if comp["status"] == "active" else "  [GEPAUZEERD]"
        url_note = f"{len(urls)} review-URL('s)" if urls else "nog geen review-URL's"
        typer.echo(
            f"[{comp['id']}] {comp['name']}{flag} ({comp['category'] or '-'}) — "
            f"{comp['n_items']} items — {url_note}"
        )


@competitor_app.command("set-urls")
def competitor_set_urls(
    name: str,
    urls: list[str] = typer.Argument(..., help="Review-pagina's (Trustpilot/bol) van dit merk."),
) -> None:
    """Review-URL's vastleggen — handig als geheugensteun bij handmatige import."""
    _, conn = _boot()
    comp = _require_competitor(conn, name)
    store.update_competitor(conn, comp["id"], review_urls=list(urls))
    typer.echo(f"✓ {len(urls)} review-URL('s) opgeslagen voor {comp['name']}.")


@competitor_app.command("pause")
def competitor_pause(name: str) -> None:
    _, conn = _boot()
    comp = _require_competitor(conn, name)
    store.set_competitor_status(conn, comp["id"], "paused")
    typer.echo(f"✓ {comp['name']} gepauzeerd.")


@competitor_app.command("resume")
def competitor_resume(name: str) -> None:
    _, conn = _boot()
    comp = _require_competitor(conn, name)
    store.set_competitor_status(conn, comp["id"], "active")
    typer.echo(f"✓ {comp['name']} weer actief.")


@competitor_app.command("set")
def competitor_set(
    name: str,
    category: str = typer.Option(None),
    notes: str = typer.Option(None),
) -> None:
    _, conn = _boot()
    comp = _require_competitor(conn, name)
    store.update_competitor(conn, comp["id"], category=category, notes=notes)
    typer.echo(f"✓ {comp['name']} bijgewerkt.")


# ── Thema's ──────────────────────────────────────────────────────────


@theme_app.command("list")
def theme_list(all_: bool = typer.Option(False, "--all", help="Ook gearchiveerde thema's.")) -> None:
    _, conn = _boot()
    for theme in store.list_themes(conn, include_archived=all_):
        flag = "" if theme["status"] == "active" else "  [GEARCHIVEERD]"
        typer.echo(f"[{theme['id']}] {theme['slug']}{flag} — {theme['description'] or ''}")


@theme_app.command("add")
def theme_add(
    slug: str = typer.Argument(..., help="Korte slug, bijv. 'abonnement-vragen'."),
    description: str = typer.Option(None, help="Uitleg (gaat mee in de AI-prompt)."),
) -> None:
    _, conn = _boot()
    store.add_theme(conn, slug.strip().casefold(), description)
    typer.echo(f"✓ Thema '{slug}' toegevoegd. De AI gebruikt het vanaf de volgende analyse.")


@theme_app.command("archive")
def theme_archive(theme_id: int) -> None:
    """Archiveren: bestaande labels blijven staan, AI gebruikt het niet meer."""
    _, conn = _boot()
    store.set_theme_status(conn, theme_id, "archived")
    typer.echo("✓ Thema gearchiveerd.")


@theme_app.command("describe")
def theme_describe(theme_id: int, description: str) -> None:
    _, conn = _boot()
    store.update_theme_description(conn, theme_id, description)
    typer.echo("✓ Beschrijving bijgewerkt.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
