"""Fase 0 — verify-first: dekt de officiële Ad Library API onze use-case?

Draai:  python verify_access.py  (of: adscout verify)

Met een META_ACCESS_TOKEN in .env bevraagt dit script één merk/pagina en
print zwart-op-wit:
  1. hoeveel commerciële NL-ads er terugkomen,
  2. welke velden daadwerkelijk gevuld zijn (dekkingspercentage per veld),
  3. één volledig voorbeeldrecord.

Zonder token: exacte setup-instructie + dezelfde analyse op de bundled
fixture, zodat de pipeline aantoonbaar werkt terwijl de token onderweg is.
Dit script schrijft NIETS naar de database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adscout.config import ADS_ARCHIVE_FIELDS, load_settings
from adscout.sources import AdSourceError, FixtureAdSource, MetaAdLibraryAPI, TokenError

FIXTURE_DIR = Path("tests/fixtures/ads")
MAX_ADS = 200

NO_TOKEN_INSTRUCTIONS = """\
────────────────────────────────────────────────────────────────────────
GEEN META_ACCESS_TOKEN GEVONDEN — setup-instructie (eenmalig, mens-werk)
────────────────────────────────────────────────────────────────────────
1. Ga naar https://developers.facebook.com/ en maak een developer-account
   (log in met je gewone Facebook-account van een van de oprichters).
2. Identiteitsverificatie: https://www.facebook.com/ID — upload een
   identiteitsbewijs. Doorlooptijd: meestal 1–2 werkdagen.
3. Accepteer de Ad Library API-voorwaarden op
   https://www.facebook.com/ads/library/api/
4. Maak een app: developers.facebook.com → My Apps → Create App
   (type "Other" / "Business", naam bijv. "Cloudplunge AdScout").
5. Haal een token op via Tools → Graph API Explorer: kies je app,
   "Generate Access Token" (geen extra permissies nodig voor ads_archive).
6. Maak er een long-lived token (±60 dagen) van — zie README,
   sectie "Token vernieuwen" voor het exacte curl-commando.
7. Zet het token in .env:  META_ACCESS_TOKEN=<token>
   en draai dit script opnieuw:  python verify_access.py

Volledige uitleg met screenshots-niveau detail: README.md.
────────────────────────────────────────────────────────────────────────
Hieronder draait dezelfde verificatie op de meegeleverde FIXTURE, zodat
je ziet wat het script straks met echte data doet.
"""


def analyze(records: list, label: str) -> None:
    print(f"\n=== Verificatie: {label} ===")
    print(f"Aantal ads opgehaald: {len(records)} (max {MAX_ADS})")
    if not records:
        print(
            "\nGEEN ads gevonden. Mogelijke oorzaken: pagina adverteert niet in dit\n"
            "land, page_id klopt niet, of de API geeft voor dit merk geen commerciële\n"
            "ads terug. Probeer een ander merk (--brand) en check de Ad Library web-UI."
        )
        return

    active = sum(1 for r in records if not r.ad_delivery_stop)
    print(f"Waarvan zonder stopdatum (actief): {active}")

    print("\nVeld-dekking (welk % van de ads heeft dit veld gevuld):")
    for field in ADS_ARCHIVE_FIELDS:
        filled = sum(1 for r in records if _has_value(r.raw.get(field)))
        bar = "█" * int(filled / len(records) * 20)
        print(f"  {field:32s} {filled / len(records) * 100:5.1f}%  {bar}")

    print("\nVoorbeeldrecord (raw API-response):")
    print(json.dumps(records[0].raw, indent=2, ensure_ascii=False)[:3000])
    print("\nCONCLUSIE: zie de dekking hierboven. Voor de tool zijn cruciaal:")
    print("  id, page_id, ad_delivery_start_time, ad_creative_bodies, ad_snapshot_url.")
    print("  Komen die (grotendeels) gevuld terug, dan dekt de API onze use-case.")


def _has_value(v) -> bool:
    return v not in (None, "", [], {})


def run(brand: str = "Cloudpillo", page_id: str | None = None, country: str = "NL") -> int:
    settings = load_settings()

    if not settings.meta_access_token:
        print(NO_TOKEN_INSTRUCTIONS)
        if not FIXTURE_DIR.exists():
            print(f"Fixture-map {FIXTURE_DIR} ontbreekt — geen demo mogelijk.")
            return 1
        src = FixtureAdSource(FIXTURE_DIR)
        candidates = src.search_pages(brand, country)
        if not candidates:
            print(f'Geen fixture-data voor "{brand}" — probeer --brand Cloudpillo')
            return 1
        records = list(src.fetch_ads([candidates[0].page_id], country))[:MAX_ADS]
        analyze(records, f"FIXTURE-MODUS — {candidates[0].page_name} ({country})")
        return 0

    try:
        src = MetaAdLibraryAPI(settings)
        if page_id:
            chosen_id, chosen_name = page_id, f"page {page_id}"
        else:
            print(f'Zoeken naar adverterende pagina\'s voor "{brand}" in {country}…')
            candidates = src.search_pages(brand, country)
            if not candidates:
                print(
                    f'\nGeen adverterende pagina\'s gevonden voor "{brand}" in {country}.\n'
                    "Probeer een ander merk (--brand) of geef direct --page-id op\n"
                    "(op te zoeken via https://www.facebook.com/ads/library/)."
                )
                return 1
            print("\nGevonden pagina's (dit is verificatie, er wordt niets opgeslagen):")
            for c in candidates[:10]:
                print(f"  {c.page_name} (page_id {c.page_id}): "
                      f"{c.active_ads} actief / {c.ads_seen} gezien")
            chosen_id, chosen_name = candidates[0].page_id, candidates[0].page_name
            print(f"\nAnalyse op de grootste adverteerder: {chosen_name}")

        records = []
        for record in src.fetch_ads([chosen_id], country):
            records.append(record)
            if len(records) >= MAX_ADS:
                break
        analyze(records, f"{chosen_name} ({country}, officiële API)")
        return 0
    except TokenError as exc:
        print(f"\nTOKEN-PROBLEEM: {exc}")
        return 2
    except AdSourceError as exc:
        print(f"\nAPI-FOUT: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", default="Cloudpillo")
    parser.add_argument("--page-id", default=None)
    parser.add_argument("--country", default="NL")
    args = parser.parse_args()
    return run(brand=args.brand, page_id=args.page_id, country=args.country)


if __name__ == "__main__":
    sys.exit(main())
