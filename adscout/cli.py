"""AdScout CLI — `adscout --help` for everything.

Managing the watchlist, categories and tags happens here or in the
dashboard; never by editing code.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import typer

from adscout import __version__, queries, report as report_mod, store
from adscout.config import Settings, load_settings, parse_countries, setup_logging
from adscout.db import open_db
from adscout.models import PageCandidate
from adscout.sources import AdSource, FixtureAdSource, MetaAdLibraryAPI, TokenError
from adscout.sources.meta import check_token

app = typer.Typer(
    name="adscout",
    help="AdScout — Meta Ads concurrentie-intelligence voor Cloudplunge.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
advertiser_app = typer.Typer(help="Watchlist beheren (concurrenten).", no_args_is_help=True)
page_app = typer.Typer(help="Facebook-pagina's van concurrenten beheren.", no_args_is_help=True)
category_app = typer.Typer(help="Categorieën en tags beheren.", no_args_is_help=True)
tag_app = typer.Typer(help="Ad-tags en AI-suggesties.", no_args_is_help=True)
creatives_app = typer.Typer(help="Gedownloade media beheren.", no_args_is_help=True)
app.add_typer(advertiser_app, name="advertiser")
app.add_typer(page_app, name="page")
app.add_typer(category_app, name="category")
app.add_typer(tag_app, name="tag")
app.add_typer(creatives_app, name="creatives")

DEFAULT_FIXTURE_DIR = Path("tests/fixtures/ads")


def _boot(verbose: bool = False) -> tuple[Settings, sqlite3.Connection]:
    settings = load_settings()
    setup_logging(settings, verbose=verbose)
    conn = open_db(settings.db_path)
    return settings, conn


def _source(settings: Settings, source: str, fixture_dir: Path) -> AdSource:
    if source == "fixture":
        return FixtureAdSource(fixture_dir)
    return MetaAdLibraryAPI(settings)


@app.callback()
def _version_callback() -> None:
    """AdScout — Meta Ads concurrentie-intelligence. Zie README.md voor uitleg."""


# ── Kerncommando's ───────────────────────────────────────────────────


@app.command()
def init() -> None:
    """DB aanmaken, migraties draaien en watchlist + taxonomie seeden."""
    settings, conn = _boot()
    n_tax = n_comp = 0
    if Path("taxonomy.seed.yaml").exists():
        n_tax = store.seed_taxonomy(conn, "taxonomy.seed.yaml")
    if Path("competitors.seed.yaml").exists():
        n_comp = store.seed_competitors(conn, "competitors.seed.yaml")
    typer.echo(f"✓ Database klaar: {settings.db_path}")
    typer.echo(f"✓ Taxonomie geseed: {n_tax} categorieën/tags")
    typer.echo(f"✓ Watchlist geseed: {n_comp} concurrenten")
    typer.echo("\nVolgende stappen:")
    typer.echo('  1. adscout resolve "Cloudpillo"   (per merk de juiste pagina bevestigen)')
    typer.echo("  2. adscout collect                (eerste dagelijkse run)")
    typer.echo("  3. adscout web                    (dashboard bekijken)")


@app.command()
def resolve(
    brand: str = typer.Argument(..., help='Merknaam, bijv. "Cloudpillo".'),
    country: list[str] = typer.Option(None, "--country", "-c", help="Landen (default: van het merk of NL)."),
    source: str = typer.Option("api", help="api of fixture (fixture = offline testen)."),
    fixture_dir: Path = typer.Option(DEFAULT_FIXTURE_DIR, help="Map met fixture-JSON."),
) -> None:
    """Kandidaat-pagina's zoeken en interactief bevestigen. Kiest nooit zelf."""
    from adscout import resolver

    settings, conn = _boot()
    adv = store.get_advertiser(conn, brand)
    countries = [c.upper() for c in country] if country else (
        parse_countries(adv["countries"]) if adv else settings.default_countries
    )
    try:
        src = _source(settings, source, fixture_dir)
        candidates = resolver.find_candidates(src, brand, countries)
    except TokenError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(2)

    if not candidates:
        typer.echo(resolver.MANUAL_LOOKUP_HELP.format(brand=brand, country=",".join(countries)))
        raise typer.Exit(1)

    typer.echo(f'\nKandidaat-pagina\'s voor "{brand}" ({",".join(countries)}):\n')
    for i, c in enumerate(candidates, 1):
        typer.echo(f"  [{i}] {c.page_name}  (page_id {c.page_id})")
        typer.echo(f"      actieve ads: {c.active_ads}, totaal gezien: {c.ads_seen}")
        if c.example_text:
            typer.echo(f"      voorbeeld: “{c.example_text}…”")
    typer.echo("\nControleer desnoods in de Ad Library web-UI: "
               "https://www.facebook.com/ads/library/")

    linked = 0
    while True:
        answer = typer.prompt(
            "\nNummer om te koppelen (meerdere mogelijk, Enter om te stoppen)",
            default="", show_default=False,
        ).strip()
        if not answer:
            break
        if not answer.isdigit() or not (1 <= int(answer) <= len(candidates)):
            typer.echo("Ongeldig nummer.")
            continue
        cand = candidates[int(answer) - 1]
        if typer.confirm(f'Koppel "{cand.page_name}" ({cand.page_id}) aan {brand}?'):
            resolver.confirm_page(conn, brand, cand, countries=countries)
            linked += 1
            typer.secho(f"✓ Gekoppeld: {cand.page_name}", fg="green")
    typer.echo(f"\nKlaar — {linked} pagina('s) gekoppeld aan {brand}.")


@app.command()
def collect(
    source: str = typer.Option("api", help="api of fixture (fixture = offline testen)."),
    fixture_dir: Path = typer.Option(DEFAULT_FIXTURE_DIR, help="Map met fixture-JSON."),
    skip_creatives: bool = typer.Option(False, help="Geen media downloaden deze run."),
    run_date: str = typer.Option(None, help="Overschrijf de run-datum (YYYY-MM-DD, voor tests)."),
    only: str = typer.Option(None, help="Alleen dit merk collecten (bijv. na een tijdelijke API-fout)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """De dagelijkse run: ads ophalen, snapshotten, veranderingen detecteren."""
    from adscout import collector, creatives

    settings, conn = _boot(verbose=verbose)

    # Guard against a silently-wrong environment: an empty watchlist means
    # this is a fresh DB (wrong werkmap? ADSCOUT_DATA_DIR verkeerd? init
    # vergeten?) — collecting would "succeed" while building history in the
    # wrong place.
    n_advertisers = conn.execute("SELECT COUNT(*) FROM advertisers").fetchone()[0]
    if n_advertisers == 0:
        typer.secho(
            f"Lege watchlist in {settings.db_path.resolve()} — is dit de juiste map?\n"
            "Draai `adscout init` in de projectmap, of zet ADSCOUT_DATA_DIR in .env.",
            fg="red",
        )
        raise typer.Exit(2)
    n_pages = conn.execute("SELECT COUNT(*) FROM advertiser_pages").fetchone()[0]
    if n_pages == 0:
        typer.secho(
            "Geen enkele concurrent heeft een gekoppelde pagina — er valt niets "
            "op te halen.\nKoppel eerst pagina's: adscout resolve \"<merknaam>\".",
            fg="red",
        )
        raise typer.Exit(2)

    try:
        src = _source(settings, source, fixture_dir)
    except TokenError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(2)

    day = date.fromisoformat(run_date) if run_date else None
    result = collector.collect(conn, src, run_date=day, only=only)
    if only and not result.per_advertiser:
        typer.secho(f'Geen actieve concurrent "{only}" gevonden — zie: adscout advertiser list', fg="red")
        raise typer.Exit(1)

    if not skip_creatives and source != "fixture" and result.new_ad_ids:
        typer.echo(f"Creatives downloaden voor {len(result.new_ad_ids)} nieuwe ads…")
        creatives.fetch_for_new_ads(
            conn, result.new_ad_ids, settings.creatives_dir,
            access_token=settings.meta_access_token,
        )

    typer.echo(f"\nRun #{result.run_id} — {result.started_at} → {result.finished_at}")
    for r in result.per_advertiser:
        if r.skipped:
            typer.echo(f"  ⏭  {r.advertiser}: overgeslagen ({r.skipped})")
        elif r.error:
            typer.secho(f"  ✗  {r.advertiser}: {r.error}", fg="red")
        elif r.warning:
            typer.secho(f"  ⚠  {r.advertiser}: {r.warning}", fg="yellow")
        else:
            typer.echo(
                f"  ✓  {r.advertiser}: {r.ads_fetched} ads, "
                f"{r.new_ads} nieuw, {r.stopped_ads} gestopt"
            )
    typer.echo(
        f"\nTotaal: {result.ads_fetched} ads, {result.new_ads} nieuw, "
        f"{result.stopped_ads} gestopt"
    )
    if result.token_error:
        typer.secho(f"\n{result.token_error}", fg="red")
        raise typer.Exit(2)
    if result.errors:
        typer.secho(f"{len(result.errors)} concurrent(en) faalde(n) — zie logs/adscout.log", fg="yellow")
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Laatste runs, token-gezondheid en aantallen per concurrent."""
    settings, conn = _boot()

    typer.echo(f"AdScout v{__version__} — database: {settings.db_path}")
    ok, msg = check_token(settings)
    typer.secho(f"Token: {msg}", fg="green" if ok else "red")

    typer.echo("\nWatchlist:")
    for adv in store.list_advertisers(conn):
        flag = "" if adv["status"] == "active" else "  [GEPAUZEERD]"
        pages = "geen pagina's — draai `adscout resolve`" if adv["n_pages"] == 0 else f"{adv['n_pages']} pagina('s)"
        typer.echo(
            f"  {adv['name']}{flag} ({adv['category'] or '-'}): "
            f"{adv['n_active_ads']} actief / {adv['n_ads']} totaal, {pages}"
        )

    typer.echo("\nLaatste runs:")
    runs = queries.last_runs(conn, limit=5)
    if not runs:
        typer.echo("  (nog geen runs — draai `adscout collect`)")
    for run in runs:
        state = "OK" if run["ok"] else "MET FOUTEN"
        typer.echo(
            f"  #{run['id']} {run['started_at']}: {state} — {run['ads_fetched']} ads, "
            f"{run['new_ads']} nieuw, {run['stopped_ads']} gestopt"
        )
        for err in json.loads(run["errors"] or "[]"):
            typer.secho(f"      fout: {err}", fg="red")
        for entry in json.loads(run["detail"] or "[]"):
            if entry.get("warning"):
                typer.secho(
                    f"      waarschuwing {entry['advertiser']}: {entry['warning']}",
                    fg="yellow",
                )

    pending = len(queries.pending_ai_suggestions(conn))
    if pending:
        typer.echo(f"\nAI-tagsuggesties wachtend op beoordeling: {pending} (zie dashboard)")


@app.command()
def report(
    weekly: bool = typer.Option(True, "--weekly", help="Weekrapport (voorlopig het enige rapporttype)."),
) -> None:
    """Weekrapport genereren (Markdown + HTML in reports/)."""
    from adscout.notify import get_notifier

    settings, conn = _boot()
    md_path, html_path = report_mod.write_weekly(conn, settings.reports_dir)
    typer.echo(f"✓ Rapport geschreven:\n  {md_path}\n  {html_path}")
    get_notifier().send_report(
        f"AdScout weekrapport {date.today().isoformat()}",
        md_path.read_text(encoding="utf-8"),
        [md_path, html_path],
    )


@app.command()
def export(
    csv_out: bool = typer.Option(False, "--csv", help="Exporteer naar CSV."),
    json_out: bool = typer.Option(False, "--json", help="Exporteer naar JSON."),
    out: Path = typer.Option(None, help="Uitvoerpad (default: exports/adscout-<datum>.<ext>)."),
) -> None:
    """Volledige data-export — geen lock-in, handig voor eigen analyses."""
    if not csv_out and not json_out:
        csv_out = True
    if out and csv_out and json_out:
        typer.secho("--out kan alleen met één formaat tegelijk (--csv óf --json).", fg="red")
        raise typer.Exit(1)
    settings, conn = _boot()
    rows = queries.export_rows(conn)
    tags_map = queries.accepted_tags_map(conn)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    def flat(row: sqlite3.Row) -> dict:
        return {
            "ad_archive_id": row["ad_archive_id"],
            "advertiser": row["advertiser_name"],
            "advertiser_category": row["advertiser_category"],
            "page_id": row["page_id"],
            "status": row["status"],
            "format": row["format"],
            "ad_delivery_start": row["ad_delivery_start"],
            "ad_delivery_stop": row["ad_delivery_stop"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "runtime_days": row["runtime_days"],
            "platforms": row["platforms"],
            "languages": row["languages"],
            "countries": row["countries"],
            "eu_reach_latest": row["eu_reach_latest"],
            "landing_url": row["landing_url"],
            "ad_library_url": report_mod.ad_library_url(row["ad_archive_id"]),
            "first_body": row["first_body"],
            "tags": "; ".join(tags_map.get(row["ad_archive_id"], [])),
        }

    written: list[Path] = []
    if csv_out:
        path = out if (out and not json_out) else Path("exports") / f"adscout-{stamp}.csv"
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
        path = out if (out and not csv_out) else Path("exports") / f"adscout-{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # All texts in one query instead of one query per ad.
        texts_by_ad: dict[str, list[dict]] = {}
        for t in conn.execute(
            "SELECT ad_id, variant_index, body, title, caption, description "
            "FROM ad_texts ORDER BY ad_id, variant_index"
        ):
            texts_by_ad.setdefault(t["ad_id"], []).append(
                {k: t[k] for k in ("variant_index", "body", "title", "caption", "description")}
            )
        payload = []
        for row in rows:
            record = flat(row)
            record["tags"] = tags_map.get(row["ad_archive_id"], [])
            record["texts"] = texts_by_ad.get(row["ad_archive_id"], [])
            payload.append(record)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    for path in written:
        typer.echo(f"✓ Export: {path} ({len(rows)} ads)")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind-adres (localhost)."),
    port: int = typer.Option(8000, help="Poort."),
) -> None:
    """Dashboard starten op http://localhost:8000."""
    import uvicorn

    from adscout.web.app import create_app

    settings, conn = _boot()
    conn.close()  # web app opens its own connections
    typer.echo(f"AdScout dashboard: http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command()
def verify(
    brand: str = typer.Option("Cloudpillo", help="Merk om mee te testen."),
    page_id: str = typer.Option(None, help="Direct een page_id testen i.p.v. zoeken."),
    country: str = typer.Option("NL", help="Land."),
) -> None:
    """Fase 0: verifieer wat de Ad Library API echt teruggeeft (of fixture-demo)."""
    from adscout import verify as verify_mod

    raise typer.Exit(verify_mod.run(brand=brand, page_id=page_id, country=country))


# ── Watchlist ────────────────────────────────────────────────────────


@advertiser_app.command("add")
def advertiser_add(
    name: str,
    category: str = typer.Option(None, help="Concurrent-categorie (zie `adscout category list`)."),
    countries: str = typer.Option(None, help="Comma-separated, bijv. NL,BE (default NL; bestaande waarde blijft staan)."),
    notes: str = typer.Option(None),
) -> None:
    """Concurrent toevoegen (pagina's daarna via `adscout resolve` of `page add`)."""
    _, conn = _boot()
    store.add_advertiser(conn, name, category, parse_countries(countries) or None, notes)
    typer.echo(f'✓ Concurrent "{name}" toegevoegd. Koppel nu een pagina: adscout resolve "{name}"')


@advertiser_app.command("list")
def advertiser_list() -> None:
    _, conn = _boot()
    for adv in store.list_advertisers(conn):
        typer.echo(
            f"[{adv['id']}] {adv['name']} ({adv['category'] or '-'}) — {adv['countries']} — "
            f"{adv['status']} — {adv['n_pages']} pagina('s), {adv['n_ads']} ads"
        )


def _require_advertiser(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    adv = store.get_advertiser(conn, name)
    if not adv:
        typer.secho(f'Concurrent "{name}" niet gevonden. Zie: adscout advertiser list', fg="red")
        raise typer.Exit(1)
    return adv


@advertiser_app.command("pause")
def advertiser_pause(name: str) -> None:
    """Pauzeren: blijft in de DB, wordt niet meer opgehaald."""
    _, conn = _boot()
    adv = _require_advertiser(conn, name)
    store.set_advertiser_status(conn, adv["id"], "paused")
    typer.echo(f"✓ {adv['name']} gepauzeerd.")


@advertiser_app.command("resume")
def advertiser_resume(name: str) -> None:
    _, conn = _boot()
    adv = _require_advertiser(conn, name)
    store.set_advertiser_status(conn, adv["id"], "active")
    typer.echo(f"✓ {adv['name']} weer actief.")


@advertiser_app.command("set")
def advertiser_set(
    name: str,
    category: str = typer.Option(None),
    countries: str = typer.Option(None, help="Comma-separated, bijv. NL,BE."),
    notes: str = typer.Option(None),
) -> None:
    """Categorie, landen of notities aanpassen."""
    _, conn = _boot()
    adv = _require_advertiser(conn, name)
    store.update_advertiser(
        conn, adv["id"],
        category=category,
        countries=parse_countries(countries) or None,
        notes=notes,
    )
    typer.echo(f"✓ {adv['name']} bijgewerkt.")


@page_app.command("add")
def page_add(
    advertiser: str,
    page_id: str,
    page_name: str = typer.Option(None, "--page-name"),
) -> None:
    """Handmatig een bevestigde pagina koppelen (bijv. uit de Ad Library web-UI)."""
    _, conn = _boot()
    adv = _require_advertiser(conn, advertiser)
    store.add_page(conn, adv["id"], page_id, page_name)
    typer.echo(f"✓ Pagina {page_id} gekoppeld aan {adv['name']}.")


@page_app.command("remove")
def page_remove(advertiser: str, page_id: str) -> None:
    _, conn = _boot()
    adv = _require_advertiser(conn, advertiser)
    store.remove_page(conn, adv["id"], page_id)
    typer.echo(f"✓ Pagina {page_id} losgekoppeld van {adv['name']}.")


@page_app.command("list")
def page_list(advertiser: str = typer.Argument(None)) -> None:
    _, conn = _boot()
    advs = [_require_advertiser(conn, advertiser)] if advertiser else store.list_advertisers(conn)
    for adv in advs:
        pages = store.pages_for(conn, adv["id"])
        typer.echo(f"{adv['name']}:")
        if not pages:
            typer.echo("  (geen pagina's — draai `adscout resolve`)")
        for p in pages:
            typer.echo(f"  {p['page_id']}  {p['page_name'] or ''}")


# ── Categorieën & tags ───────────────────────────────────────────────


@category_app.command("add")
def category_add(
    name: str,
    type_: str = typer.Option("ad_tag", "--type", help="ad_tag of advertiser_category."),
    group: str = typer.Option(None, help="Tag-groep (hook/offer/format/angle/audience)."),
    description: str = typer.Option(None),
) -> None:
    _, conn = _boot()
    if type_ not in ("ad_tag", "advertiser_category"):
        typer.secho("type moet ad_tag of advertiser_category zijn", fg="red")
        raise typer.Exit(1)
    store.add_category(conn, name, type_, group, description)
    typer.echo(f"✓ Categorie '{name}' ({type_}) toegevoegd.")


@category_app.command("list")
def category_list(type_: str = typer.Option(None, "--type")) -> None:
    _, conn = _boot()
    for c in store.list_categories(conn, type_):
        group = f" [{c['tag_group']}]" if c["tag_group"] else ""
        typer.echo(f"[{c['id']}] ({c['type']}){group} {c['name']} — {c['description'] or ''}")


@category_app.command("rename")
def category_rename(category_id: int, new_name: str) -> None:
    _, conn = _boot()
    store.rename_category(conn, category_id, new_name)
    typer.echo(f"✓ Categorie {category_id} hernoemd naar '{new_name}'.")


@category_app.command("delete")
def category_delete(category_id: int) -> None:
    _, conn = _boot()
    row = conn.execute("SELECT name, type FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not row:
        typer.secho("Categorie niet gevonden.", fg="red")
        raise typer.Exit(1)
    if typer.confirm(f"Verwijder '{row['name']}' ({row['type']}) inclusief alle tag-koppelingen?"):
        store.delete_category(conn, category_id)
        typer.echo("✓ Verwijderd.")


@creatives_app.command("backfill")
def creatives_backfill(
    limit: int = typer.Option(200, help="Max aantal ads per keer."),
) -> None:
    """Media alsnog downloaden voor ads zonder creatives (bijv. na een
    mislukte download of een --skip-creatives run)."""
    from adscout import creatives

    settings, conn = _boot()
    ad_ids = creatives.ads_without_creatives(conn, limit=limit)
    if not ad_ids:
        typer.echo("Geen ads zonder creatives gevonden — niets te doen.")
        return
    typer.echo(f"Backfill voor {len(ad_ids)} ads…")
    n = creatives.fetch_for_new_ads(
        conn, ad_ids, settings.creatives_dir, access_token=settings.meta_access_token
    )
    typer.echo(f"✓ {n} bestanden gedownload. (Niet alles lukt: verlopen snapshots blijven leeg.)")


@tag_app.command("suggest")
def tag_suggest(limit: int = typer.Option(25, help="Max aantal ads per keer.")) -> None:
    """AI-tagsuggesties genereren (vereist ANTHROPIC_API_KEY in .env)."""
    from adscout.tagging import ai

    settings, conn = _boot()
    try:
        n = ai.suggest_for_untagged(conn, settings, limit=limit)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="yellow")
        raise typer.Exit(1)
    typer.echo(f"✓ Suggesties gegenereerd voor {n} ads. Beoordeel ze in het dashboard (Beheer → AI-suggesties).")


@tag_app.command("pending")
def tag_pending() -> None:
    """Toon AI-suggesties die op beoordeling wachten."""
    _, conn = _boot()
    rows = queries.pending_ai_suggestions(conn)
    if not rows:
        typer.echo("Geen wachtende suggesties.")
        return
    for r in rows:
        typer.echo(
            f"ad {r['ad_id']} ({r['advertiser_name']}): {r['tag_name']} "
            f"(confidence {r['confidence']:.0%})"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
