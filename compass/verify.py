"""Verify-first: werkt de koppeling per bron, vóór er iets wordt gebouwd?

Draai:  compass verify          (alle drie de bronnen)
        compass verify shopify  (één bron)

Met credentials in .env bevraagt dit script de echte API per bron en
print zwart-op-wit wat er terugkomt: aantallen, of de velden die Compass
nodig heeft gevuld zijn, en één voorbeeldrecord.

Zonder credentials: de exacte setup-instructie per bron (stap voor stap,
met verwijzing naar compass/README.md) plus dezelfde analyse op de
meegeleverde fixture-data, zodat de pipeline aantoonbaar werkt terwijl
de toegang geregeld wordt.

Dit script schrijft NIETS naar de database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from compass.config import Settings, load_settings
from compass.models import fmt_eur
from compass.sources import (
    BolSource,
    CredentialsError,
    FixtureBolOrders,
    FixtureMetaSpend,
    FixtureShopifyOrders,
    MetaInsightsSource,
    ShopifySource,
    SourceError,
)
from compass.sources import bol as bol_mod
from compass.sources import meta_insights as meta_mod
from compass.sources import shopify as shopify_mod
from compass.sources.fixture import DEFAULT_FIXTURE_DIR

SOURCES = ("shopify", "meta", "bol")

MAX_EXAMPLE_CHARS = 3000

# Raw API fields checked in the coverage analysis: exactly what the parse
# functions read, so a founder sees whether the API delivers what we need.
SHOPIFY_FIELDS = (
    "id",
    "created_at",
    "total_price",
    "total_tax",
    "financial_status",
    "line_items",
    "payment_gateway_names",
    "customer",
    "refunds",
)
META_FIELDS = (
    "campaign_id",
    "campaign_name",
    "spend",
    "impressions",
    "clicks",
    "actions",
    "action_values",
    "date_start",
)
BOL_FIELDS = ("orderId", "orderPlacedDateTime", "orderItems")

FIXTURE_FILES = {
    "shopify": "shopify_orders.json",
    "meta": "meta_insights.json",
    "bol": "bol_orders.json",
}

_RULE = "─" * 72

NO_SHOPIFY_INSTRUCTIONS = f"""\
{_RULE}
GEEN SHOPIFY-CREDENTIALS GEVONDEN — setup-instructie (eenmalig, mens-werk)
{_RULE}
1. Log in op de Shopify-admin en ga naar Instellingen → Apps en
   verkoopkanalen → Apps ontwikkelen (menu kan iets anders heten).
2. Maak een app aan met de naam "Compass".
3. Geef de app alléén deze Admin API-scopes: read_orders, read_products
   en read_inventory — Compass leest, en wijzigt nooit iets.
4. Installeer de app en kopieer het Admin API access token (shpat_…).
   LET OP: Shopify toont dit token maar één keer.
5. Zet in .env:  SHOPIFY_SHOP=<shop>.myshopify.com
                 SHOPIFY_ACCESS_TOKEN=<token>
6. Draai daarna opnieuw:  compass verify shopify

Volledige uitleg: compass/README.md, sectie "Shopify custom app aanmaken".
{_RULE}
Hieronder draait dezelfde verificatie op de meegeleverde FIXTURE, zodat
je ziet wat het script straks met echte data doet.
"""

NO_META_INSTRUCTIONS = f"""\
{_RULE}
GEEN META-CREDENTIALS GEVONDEN — setup-instructie (eenmalig, mens-werk)
{_RULE}
1. Hergebruik de AdScout-app op https://developers.facebook.com/
   (zelfde app, zelfde token) en voeg het product "Marketing API" toe.
2. Haal via Tools → Graph API Explorer een token op met de permissie
   ads_read en maak er een long-lived token (±60 dagen) van — zie de
   root-README, sectie "Token vernieuwen".
3. Zoek het advertentieaccount-ID op in Ads Manager
   (https://adsmanager.facebook.com): het nummer in de URL of in het
   accountoverzicht.
4. Zet in .env:  META_ACCESS_TOKEN=<token>
                 META_AD_ACCOUNT_ID=act_<nummer>
5. Draai daarna opnieuw:  compass verify meta

Volledige uitleg: compass/README.md, sectie "Meta Marketing API koppelen".
{_RULE}
Hieronder draait dezelfde verificatie op de meegeleverde FIXTURE, zodat
je ziet wat het script straks met echte data doet.
"""

NO_BOL_INSTRUCTIONS = f"""\
{_RULE}
GEEN BOL-CREDENTIALS GEVONDEN — setup-instructie (eenmalig, mens-werk)
{_RULE}
1. Log in op het partnerplatform: https://partner.bol.com/
2. Ga naar Instellingen → Diensten → API-instellingen (menu kan iets
   anders heten) en maak client credentials aan.
3. Zet in .env:  BOL_CLIENT_ID=<client id>
                 BOL_CLIENT_SECRET=<client secret>
4. Draai daarna opnieuw:  compass verify bol

Volledige uitleg: compass/README.md, sectie "bol Retailer API koppelen".
{_RULE}
Hieronder draait dezelfde verificatie op de meegeleverde FIXTURE, zodat
je ziet wat het script straks met echte data doet.
"""

INSTRUCTIONS = {
    "shopify": NO_SHOPIFY_INSTRUCTIONS,
    "meta": NO_META_INSTRUCTIONS,
    "bol": NO_BOL_INSTRUCTIONS,
}


# ── analysis helpers (shared shape for fixture and live output) ──────


def _has_value(value) -> bool:
    return value not in (None, "", [], {})


def _print_coverage(items: list[dict], fields: tuple[str, ...], noun: str) -> None:
    print(f"\nVeld-dekking (welk % van de {noun} heeft dit veld gevuld):")
    for field in fields:
        filled = sum(1 for item in items if _has_value(item.get(field)))
        bar = "█" * int(filled / len(items) * 20)
        print(f"  {field:32s} {filled / len(items) * 100:5.1f}%  {bar}")


def _print_example(item: dict) -> None:
    print("\nVoorbeeldrecord (raw API-vorm):")
    print(json.dumps(item, indent=2, ensure_ascii=False)[:MAX_EXAMPLE_CHARS])


def _fixture_path(name: str) -> Path | None:
    path = DEFAULT_FIXTURE_DIR / FIXTURE_FILES[name]
    if not path.is_file():
        print(f"Fixture-bestand {path} ontbreekt — geen demo mogelijk.")
        return None
    return path


def _analyze_shopify_fixture() -> int:
    if _fixture_path("shopify") is None:
        return 1
    items = FixtureShopifyOrders.from_dir(DEFAULT_FIXTURE_DIR).items
    print("\n=== Verificatie: FIXTURE-MODUS — Shopify (demo-data) ===")
    if not items:
        print("Geen orders in de fixture — geen demo mogelijk.")
        return 1
    records = [r for r in map(shopify_mod.parse_order, items) if r is not None]
    skipped = len(items) - len(records)
    print(
        f"Aantal orders geparsed: {len(records)} van {len(items)} "
        f"({skipped} testorder(s) overgeslagen)"
    )
    paid = [r for r in records if r.status == "paid"]
    revenue = sum(max(r.gross_cents - r.refunded_cents, 0) for r in paid)
    print(
        f"Waarvan betaald: {len(paid)}, samen {fmt_eur(revenue)} incl. btw "
        f"(refunds al verrekend)."
    )
    _print_coverage(items, SHOPIFY_FIELDS, "orders")
    _print_example(items[0])
    print("\nCONCLUSIE: cruciaal zijn id, created_at, total_price, total_tax,")
    print("  financial_status en line_items. Komen die gevuld terug, dan kan")
    print("  Compass omzet, btw en refunds exact narekenen.")
    return 0


def _analyze_meta_fixture() -> int:
    if _fixture_path("meta") is None:
        return 1
    rows = FixtureMetaSpend.from_dir(DEFAULT_FIXTURE_DIR).items
    print("\n=== Verificatie: FIXTURE-MODUS — Meta Insights (demo-data) ===")
    if not rows:
        print("Geen campagne-dagen in de fixture — geen demo mogelijk.")
        return 1
    records = [meta_mod.parse_insight_row(row) for row in rows]
    campaigns = {r.campaign_id for r in records}
    days = {r.day for r in records}
    total = sum(r.spend_cents for r in records)
    print(
        f"Aantal campagne-dagen geparsed: {len(records)} "
        f"({len(campaigns)} campagnes, {len(days)} dagen), {fmt_eur(total)} spend."
    )
    if any(r.meta_purchases or r.meta_purchase_value_cents for r in records):
        print("Aankopen en aankoopwaarde 'volgens Meta' komen mee — attributie werkt.")
    else:
        print(
            "Geen aankopen/aankoopwaarde in de rijen (pixel niet gekoppeld?) — "
            "ROAS 'volgens Meta' blijft dan leeg."
        )
    _print_coverage(rows, META_FIELDS, "rijen")
    _print_example(rows[0])
    print("\nCONCLUSIE: cruciaal zijn campaign_id, date_start en spend; de")
    print("  actions/action_values leveren Meta's eigen aankoopclaim — die toont")
    print("  Compass altijd NAAST de werkelijke Shopify+bol-cijfers.")
    return 0


def _analyze_bol_fixture() -> int:
    if _fixture_path("bol") is None:
        return 1
    items = FixtureBolOrders.from_dir(DEFAULT_FIXTURE_DIR).items
    print("\n=== Verificatie: FIXTURE-MODUS — bol (demo-data) ===")
    if not items:
        print("Geen orders in de fixture — geen demo mogelijk.")
        return 1
    records = [bol_mod.parse_order_detail(item) for item in items]
    cancelled = sum(1 for r in records if r.status == "refunded")
    paid = [r for r in records if r.status == "paid"]
    revenue = sum(max(r.gross_cents - r.refunded_cents, 0) for r in paid)
    print(
        f"Aantal bol-orders geparsed: {len(records)} "
        f"(waarvan {cancelled} geannuleerd), {fmt_eur(revenue)} betaalde omzet incl. btw."
    )
    _print_coverage(items, BOL_FIELDS, "orders")
    _print_example(items[0])
    print("\nCONCLUSIE: cruciaal zijn orderId, orderPlacedDateTime en de")
    print("  orderItems (quantity, unitPrice, quantityCancelled). bol maskeert")
    print("  de koper — die telt in Compass daarom altijd als nieuwe klant.")
    return 0


_FIXTURE_ANALYSES = {
    "shopify": _analyze_shopify_fixture,
    "meta": _analyze_meta_fixture,
    "bol": _analyze_bol_fixture,
}


# ── live verification (credentials present) ──────────────────────────


def _verify_live(name: str, settings: Settings) -> int:
    """Run the adapter's own verify() against the real API and print it.

    Exit contribution: 0 ok, 1 issues, 2 credential problems — the quick
    check_access/check_token summary catches the credential case early so
    a dead token gives one clear message instead of a wall of errors.
    """
    print(f"\n=== Verificatie: {name} (live API) ===")
    try:
        if name == "shopify":
            ok, message = shopify_mod.check_access(settings)
            print(f"Snelle check: {message}")
            if not ok and message == shopify_mod.CREDENTIALS_HELP:
                return 2
            report = ShopifySource(settings).verify()
        elif name == "meta":
            ok, message = meta_mod.check_token(settings)
            print(f"Snelle check: {message}")
            if not ok and message == meta_mod.TOKEN_HELP:
                return 2
            report = MetaInsightsSource(settings).verify()
        else:
            report = BolSource(settings).verify()
    except CredentialsError as exc:
        print(f"\nCREDENTIALS-PROBLEEM ({name}): {exc}")
        return 2
    except SourceError as exc:
        print(f"\nAPI-FOUT ({name}): {exc}")
        return 1

    for line in report.lines:
        print(f"  {line}")
    if not report.ok:
        print(f"\nEr ging iets mis bij {name} — zie de regels hierboven.")
        return 1
    return 0


def _verify_source(name: str, settings: Settings) -> int:
    has_credentials = {
        "shopify": settings.has_shopify,
        "meta": settings.has_meta,
        "bol": settings.has_bol,
    }[name]()
    if not has_credentials:
        print(INSTRUCTIONS[name])
        return _FIXTURE_ANALYSES[name]()
    return _verify_live(name, settings)


def run(source: str | None = None) -> int:
    """Verify one source or all three. Exit code: 0 alles ok / 1 problemen /
    2 credential-problemen (het slechtste resultaat telt)."""
    if source is not None and source not in SOURCES:
        print(f"Onbekende bron {source!r} — kies uit: {', '.join(SOURCES)}.")
        return 1
    settings = load_settings()
    exit_code = 0
    for name in [source] if source else list(SOURCES):
        exit_code = max(exit_code, _verify_source(name, settings))
    return exit_code


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
