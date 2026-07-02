# AdScout

Interne tool van Cloudplunge: dagelijks automatisch bijhouden welke Meta-advertenties (Facebook/Instagram) onze concurrenten draaien, welke het langst lopen (de "winnaars") en wat er per week verandert.

Deze README is geschreven voor onszelf, twee niet-technische oprichters. Uitgangspunt: **over twee jaar moet je dit nog kunnen draaien zonder hulp**. Lees minimaal secties 1 t/m 4; de rest is naslag.

## Inhoudsopgave

1. [Wat is AdScout](#1-wat-is-adscout)
2. [Snelstart](#2-snelstart)
3. [Meta developer-app en token](#3-meta-developer-app-en-token)
4. [Dagelijkse run instellen](#4-dagelijkse-run-instellen)
5. [Concurrenten toevoegen en pagina's resolven](#5-concurrenten-toevoegen-en-paginas-resolven)
6. [Categorieën en tags beheren](#6-categorieën-en-tags-beheren)
7. [Weekrapport en export](#7-weekrapport-en-export)
8. [Backup en herstel](#8-backup-en-herstel)
9. [Troubleshooting](#9-troubleshooting)
10. [Onderhoud](#10-onderhoud)
11. [Compliance en fair use](#11-compliance-en-fair-use)

---

## 1. Wat is AdScout

AdScout haalt elke dag via de officiële **Meta Ad Library API** alle advertenties op van de concurrenten op onze watchlist en bewaart daar een historie van die de Ad Library zelf niet biedt. Omdat Meta voor commerciële advertenties geen budget of impressies vrijgeeft, gebruikt AdScout **looptijd** als maatstaf: een advertentie die weken blijft draaien, werkt blijkbaar. Het resultaat bekijk je in een lokaal dashboard, een wekelijks rapport en exports.

**De architectuur in woorden:**

```
bronnen (Meta Ad Library API, of fixture voor offline demo)
        │  via de AdSource-adapter
        ▼
collector (dagelijkse run: ophalen, snapshotten, veranderingen detecteren)
        ▼
SQLite-database (data/adscout.db) + creatives op schijf (data/creatives/)
        ▼
dashboard (adscout web)  ·  weekrapport (adscout report)  ·  export (adscout export)
```

Belangrijk ontwerpprincipe: elke databron zit achter dezelfde **AdSource-interface** (`adscout/sources/base.py`). Willen we later een betaalde ad-intelligence-provider gebruiken, dan schrijven we alleen een nieuwe adapter — collector, database, dashboard en rapporten blijven ongewijzigd. Zie [sectie 10](#10-onderhoud).

Alles draait lokaal op je eigen computer (of optioneel via GitHub Actions, zie sectie 4). Er is geen server, geen abonnement en geen cloud-database.

---

## 2. Snelstart

### Stap 1 — Python 3.11 of nieuwer installeren

- **macOS:** download de installer op [python.org/downloads](https://www.python.org/downloads/) en klik je erdoorheen. Of, als je Homebrew hebt: `brew install python`.
- **Windows:** download de installer op [python.org/downloads](https://www.python.org/downloads/). Vink tijdens de installatie **"Add python.exe to PATH"** aan.

Check daarna in een terminal (macOS: Terminal-app; Windows: PowerShell):

```bash
python3 --version    # Windows: python --version
```

Dit moet 3.11 of hoger tonen.

### Stap 2 — AdScout installeren

Open een terminal in de map van dit project (`ads-scraper`) en voer uit:

```bash
python3 -m venv .venv                # eenmalig: geïsoleerde Python-omgeving
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env                 # Windows: copy .env.example .env
```

> Elke keer dat je een nieuwe terminal opent, activeer je eerst weer de venv met `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`). Daarna werkt het commando `adscout`.

### Stap 3 — Initialiseren en draaien

```bash
adscout init                  # database aanmaken + watchlist en taxonomie seeden
python verify_access.py       # checkt wat de Ad Library API echt teruggeeft
adscout resolve "Cloudpillo"  # per merk de juiste Facebook-pagina bevestigen
adscout collect               # eerste dagelijkse run
adscout web                   # dashboard op http://localhost:8000 (stoppen: Ctrl+C)
```

Voor `adscout resolve` en `adscout collect` heb je een Meta-token nodig — zie [sectie 3](#3-meta-developer-app-en-token).

### Zonder token: offline demo

**Alles werkt ook zonder token**, op meegeleverde voorbeelddata (de "fixture"). Handig om de tool te leren kennen terwijl je identiteitsverificatie bij Meta nog loopt:

```bash
adscout init
python verify_access.py                   # zonder token draait dit vanzelf op de fixture
adscout resolve "Cloudpillo" --source fixture
adscout collect --source fixture
adscout web
```

---

## 3. Meta developer-app en token

Eenmalig mensenwerk (±30 minuten actief, plus 1–2 werkdagen wachten op Meta). Daarna alleen elke ±60 dagen het token vernieuwen.

### Stap voor stap

1. **Developer-account aanmaken.** Ga naar [developers.facebook.com](https://developers.facebook.com/) en log in met het gewone Facebook-account van een van ons. Kies "Get Started" en doorloop de registratie.
2. **Identiteit verifiëren.** Ga naar [facebook.com/ID](https://www.facebook.com/ID) en upload een identiteitsbewijs. Dit is verplicht voor de Ad Library API. **Verwachte doorlooptijd: 1–2 werkdagen.** Je krijgt een melding van Facebook zodra het goedgekeurd is.
3. **Ad Library API-voorwaarden accepteren.** Ga naar [facebook.com/ads/library/api](https://www.facebook.com/ads/library/api/) en accepteer daar de voorwaarden ("Terms of Service").
4. **App aanmaken.** Op developers.facebook.com: **My Apps → Create App**. Kies als type "Other" / "Business". Naam bijvoorbeeld `Cloudplunge AdScout`. Verder zijn geen producten of instellingen nodig.
5. **Token ophalen via de Graph API Explorer.** Ga naar **Tools → [Graph API Explorer](https://developers.facebook.com/tools/explorer/)**, kies rechtsboven jouw app en klik **"Generate Access Token"**. Er zijn geen extra permissies nodig voor de Ad Library. Kopieer het token — let op: dit korte token is maar 1–2 uur geldig, dus doe meteen stap 6.
6. **Long-lived token maken (±60 dagen geldig).** Je hebt nodig: je **App ID** en **App Secret** (in je app onder **Settings → Basic**) en het korte token uit stap 5. Voer dan dit exacte commando uit in de terminal (alles op één regel, vervang de drie JOUW_… waarden):

   ```bash
   curl -s "https://graph.facebook.com/v23.0/oauth/access_token?grant_type=fb_exchange_token&client_id=JOUW_APP_ID&client_secret=JOUW_APP_SECRET&fb_exchange_token=JOUW_KORTE_TOKEN"
   ```

   Het antwoord is een stukje JSON met daarin `"access_token": "..."` — dát is je long-lived token.
7. **Token in `.env` zetten.** Open het bestand `.env` in de projectmap en vul in:

   ```
   META_ACCESS_TOKEN=hier-je-long-lived-token
   ```

   `.env` staat in `.gitignore` en komt dus nooit in git terecht.
8. **Controleren:**

   ```bash
   adscout status             # toont "Token: token geldig (account: …)"
   python verify_access.py    # bevraagt de echte API en toont de veld-dekking
   ```

### Token vernieuwen

- Long-lived tokens verlopen na **±60 dagen**. Zet een terugkerende agenda-herinnering op elke ~50 dagen.
- Symptoom van een verlopen token: `adscout collect` stopt met een rode melding over **Meta error 190**, en `adscout status` toont de token-gezondheid ("Token: …").
- Vernieuwen = stap 5 t/m 7 herhalen: nieuw kort token via de Graph API Explorer, met het curl-commando omwisselen voor een long-lived token, en het nieuwe token in `.env` plakken. Klaar in 5 minuten.
- Gebruik je de GitHub Actions-route uit sectie 4? Vernieuw dan óók de secret `META_ACCESS_TOKEN` op GitHub.

---

## 4. Dagelijkse run instellen

AdScout leeft van dagelijkse metingen (daar komt de looptijd-historie vandaan). Kies één van de twee routes. Route (a) is het eenvoudigst als je computer 's ochtends toch aanstaat; route (b) draait in de cloud en mist nooit een dag.

Eén gemiste dag is overigens geen ramp: de eerstvolgende run haalt de actuele stand gewoon weer op, en dubbel draaien op één dag kan geen kwaad (geen duplicaten).

### Route (a): op je eigen computer (cron of launchd)

Het kant-en-klare script `ops/collect-cron.sh` doet alles: het gaat naar de projectmap, activeert de venv, draait `adscout collect`, maakt op maandag het weekrapport en logt alles naar `logs/cron.log`.

**Cron (macOS en Linux).** Open je crontab:

```bash
crontab -e
```

en voeg deze regel toe (vervang het pad door jouw echte projectpad; op macOS bijvoorbeeld `/Users/jouwnaam/ads-scraper`):

```
0 7 * * * /bin/bash /Users/jouwnaam/ads-scraper/ops/collect-cron.sh
```

Dat is: elke dag om 07:00. Controleer de volgende ochtend `logs/cron.log`.

**Launchd (de nettere macOS-variant).** Voordeel boven cron: sliep je Mac om 07:00, dan draait launchd de run alsnog zodra hij wakker wordt. Installatie:

```bash
cp ops/nl.cloudplunge.adscout.plist ~/Library/LaunchAgents/
open -e ~/Library/LaunchAgents/nl.cloudplunge.adscout.plist
```

Vervang in dat bestand de **drie** plekken met `/REPLACE/PAD/NAAR/ads-scraper` door jouw echte projectpad, sla op, en laad de taak:

```bash
launchctl load ~/Library/LaunchAgents/nl.cloudplunge.adscout.plist
```

Direct testen: `launchctl start nl.cloudplunge.adscout` en dan `logs/cron.log` bekijken. Uitzetten: `launchctl unload ~/Library/LaunchAgents/nl.cloudplunge.adscout.plist`.

### Route (b): GitHub Actions (cloud, computer mag uit)

De workflow staat klaar in [`.github/workflows/adscout-collect.yml`](.github/workflows/adscout-collect.yml) en draait dagelijks om 05:15 UTC (07:15 NL-zomertijd) plus op elk moment handmatig via **Actions → AdScout daily collect → Run workflow**.

> **WAARSCHUWING: de repository MOET privé zijn.** De workflow commit de database met concurrentiedata terug naar de repo, en het Meta-token staat in de repo-secrets. Check op GitHub onder Settings dat er "Private" bij de repo staat vóór je deze route gebruikt.

Setup:

1. Push dit project naar een **privé** GitHub-repository.
2. Zet het token als secret: op GitHub **Settings → Secrets and variables → Actions → New repository secret**, naam `META_ACCESS_TOKEN`, waarde je long-lived token uit sectie 3.
3. **Eenmalige bootstrap van de database.** De cloud-runner heeft je watchlist met bevestigde pagina's nodig; die staat in je lokale database. Draai lokaal eerst `adscout init` en `adscout resolve` voor je concurrenten (sectie 5), en commit daarna de database één keer expliciet mee (hij is normaal gitignored, vandaar `-f`):

   ```bash
   git add -f data/adscout.db
   git commit -m "Bootstrap: geïnitialiseerde database met watchlist"
   git push
   ```

   Zonder deze stap weigert de workflow bewust te draaien (foutmelding in de Actions-log) — anders zou hij dagelijks "groen" zijn terwijl hij tegen een lege database niets verzamelt.
4. Klaar — de workflow draait vanaf nu dagelijks vanzelf. Test hem direct via **Actions → AdScout daily collect → Run workflow**.

Hoe het werkt: elke run installeert AdScout op een tijdelijke machine, draait `adscout collect` (en op maandag `adscout report --weekly`) en **commit daarna `data/` en `reports/` terug naar de repo** met een commit als `[data] daily collect 2026-07-01`. De repo zelf is dus de opslag: de SQLite-database en de creatives blijven bewaard en de historie groeit elke dag aan. Wil je de data lokaal bekijken, doe dan eerst `git pull` en start `adscout web`.

Verloopt het token, dan zie je de waarschuwing in de Actions-log; vernieuw dan de secret (sectie 3, "Token vernieuwen").

---

## 5. Concurrenten toevoegen en pagina's resolven

De watchlist beheer je via de CLI of het dashboard — nooit door code aan te passen.

**Via de CLI:**

```bash
adscout advertiser add "Merknaam" --category supplement --countries NL,BE
adscout advertiser list
adscout advertiser pause "Merknaam"     # tijdelijk niet meer ophalen (data blijft)
adscout advertiser resume "Merknaam"
adscout advertiser set "Merknaam" --category wellness --notes "check Q4"
```

**Via het dashboard:** start `adscout web`, ga naar **Beheer** en gebruik het formulier "Concurrent toevoegen".

### Pagina's resolven — de belangrijkste stap

Een concurrent zonder gekoppelde Facebook-pagina wordt bij `adscout collect` overgeslagen. De koppeling maak je met:

```bash
adscout resolve "Merknaam"
```

Dit zoekt adverterende pagina's die bij de merknaam passen en toont per kandidaat bewijs: paginanaam, page_id, aantal actieve advertenties en een voorbeeldtekst. **Jij kiest en bevestigt; AdScout gokt nooit zelf.** Dat is bewust: generieke namen zoals **Ella** of **Oana** matchen tientallen pagina's, en één verkeerde koppeling betekent maanden vervuilde data. Twijfel je, check de kandidaat dan eerst in de [Ad Library web-UI](https://www.facebook.com/ads/library/).

Ken je het page_id al (bijvoorbeeld opgezocht in de Ad Library web-UI, zie [sectie 9](#9-troubleshooting)), dan kan het ook direct:

```bash
adscout page add "Merknaam" 123456789012345 --page-name "Officiële paginanaam"
adscout page list                        # alle koppelingen bekijken
adscout page remove "Merknaam" 123456789012345
```

In het dashboard kan dit ook onder **Beheer** ("Pagina koppelen"). Eén merk mag meerdere pagina's hebben (bijvoorbeeld een NL- en een BE-pagina).

---

## 6. Categorieën en tags beheren

Er zijn twee soorten labels, beide volledig door onszelf te beheren:

- **advertiser_category** — één categorie per concurrent (supplement, sleep-comfort, wellness, …).
- **ad_tag** — meerdere tags per advertentie, gegroepeerd (hook / offer / format / angle / audience).

**Via de CLI:**

```bash
adscout category list
adscout category add "hook: schaarste" --type ad_tag --group hook --description "Opent met op=op / beperkte tijd."
adscout category add "meubels" --type advertiser_category
adscout category rename 12 "hook: urgentie"
adscout category delete 12
```

**Via het dashboard:** onder **Beheer → Categorieën**. Tags op een specifieke advertentie zet je op de detailpagina van die advertentie.

Bij `adscout init` wordt een startset geladen uit `taxonomy.seed.yaml`; daarna is de database leidend en overschrijft opnieuw seeden je eigen aanpassingen niet.

### AI-tagsuggesties (optioneel, standaard uit)

AdScout kan met de Claude API tag-**suggesties** genereren voor nog ongetagde advertenties. AI zet nooit zelf definitieve tags: elke suggestie wacht op jouw akkoord in het dashboard. **Alles werkt volledig zonder deze functie.**

Aanzetten:

1. `pip install -r requirements-ai.txt` (installeert het optionele `anthropic`-pakket).
2. Zet in `.env`: `ANTHROPIC_API_KEY=...` (aan te maken op [console.anthropic.com](https://console.anthropic.com/)).
3. Draai:

   ```bash
   adscout tag suggest --limit 25    # suggesties genereren
   adscout tag pending               # wat wacht er op beoordeling
   ```

4. Beoordeel de suggesties in het dashboard (**Beheer → AI-suggesties**): accepteren of afwijzen.

Uitzetten = de key uit `.env` halen. Meer wordt er niet aan AI gedaan.

---

## 7. Weekrapport en export

**Weekrapport** — wat veranderde er de afgelopen 7 dagen en wie zijn de langstlopers per categorie:

```bash
adscout report
```

Dit schrijft twee bestanden in de map `reports/`: `weekly-<datum>.md` (Markdown) en `weekly-<datum>.html` (open je in de browser, ook prima te printen of door te sturen). De geplande run uit sectie 4 maakt dit rapport automatisch elke maandag.

**Export** — alle data zonder lock-in, voor eigen analyses in Excel/Numbers/Google Sheets:

```bash
adscout export --csv                       # exports/adscout-<datum>.csv
adscout export --json                      # zelfde data + alle tekstvarianten
adscout export --csv --out ~/Desktop/ads.csv
```

Per advertentie zit er onder meer in: adverteerder, status, formaat, start/stop, looptijd in dagen, platforms, landen, EU-bereik, eerste advertentietekst, tags en een permanente Ad Library-link.

---

## 8. Backup en herstel

Alles wat waardevol is, staat in drie plekken:

| Wat | Waarom |
|---|---|
| `data/adscout.db` | de volledige database: watchlist, advertenties, historie, tags |
| `data/creatives/` | gedownloade beelden/video's (media-links van Meta verlopen, deze kopieën niet) |
| `.env` | je tokens en instellingen |

**Backup maken:** sluit eerst het dashboard en wacht tot een lopende `collect` klaar is, en kopieer dan simpelweg de hele `data/`-map plus het `.env`-bestand naar een andere plek. Bijvoorbeeld:

```bash
cp -r data ~/Backups/adscout-backup-$(date +%F)
cp .env ~/Backups/adscout-backup-$(date +%F)/
```

**Advies: zet wekelijks een kopie op een cloud-drive** (Google Drive, Dropbox, iCloud). Dan overleeft de data ook een kapotte of gestolen laptop. Zet er een vaste weekherinnering voor, of leg de `data/`-map in een map die je cloud-drive al synchroniseert.

**Herstellen:** installeer AdScout volgens de Snelstart (sectie 2, stap 1–2), zet de gebackupte `data/`-map en `.env` terug in de projectmap, en draai `adscout status` om te controleren dat alles er weer is. Dat is alles — er is geen aparte import-stap.

Gebruik je de GitHub Actions-route uit sectie 4, dan ís de (privé) repo je backup; alleen `.env` bewaar je dan nog apart.

---

## 9. Troubleshooting

**"Token verlopen of ongeldig (Meta error 190)"**
Long-lived tokens verlopen na ±60 dagen. `adscout status` toont de token-gezondheid. Oplossing: token vernieuwen, zie [sectie 3](#3-meta-developer-app-en-token) onder "Token vernieuwen".

**Rate limit / "Meta API tijdelijk niet beschikbaar"**
Meta begrenst het aantal API-calls per app. AdScout leest de `X-App-Usage`-header en gaat vanzelf langzamer rijden als het quotum bijna vol zit, en probeert het bij tijdelijke fouten automatisch opnieuw met oplopende wachttijden (backoff). Faalt een run alsnog, dan is de oplossing simpel: **gewoon later opnieuw `adscout collect` draaien.** Runs zijn idempotent — dubbel draaien geeft geen dubbele data.

**Lege resultaten voor een merk**
Drie gebruikelijke oorzaken: (1) de pagina adverteert op dit moment simpelweg niet; (2) het land klopt niet — check `adscout advertiser list` en pas aan met `adscout advertiser set "Merk" --countries NL,BE`; (3) de API geeft voor dit merk minder terug dan de web-UI toont (dekking verschilt soms). Vergelijk altijd even met de [Ad Library web-UI](https://www.facebook.com/ads/library/) en test gericht met `python verify_access.py --brand "Merk"`.

**page_id kwijt of niet te vinden via resolve**
Handmatig opzoeken in de Ad Library web-UI:
1. Open https://www.facebook.com/ads/library/ in je browser.
2. Kies het land en categorie "Alle advertenties", zoek op de merknaam.
3. Klik een advertentie van het juiste merk en klik dan op de paginanaam.
4. Het page_id staat in de URL van die pagina (of via "Info en advertenties").
5. Koppelen: `adscout page add "Merk" <page_id> --page-name "Naam"`.

**Ontbrekende thumbnails / creatives**
Media wordt gedownload zodra een ad voor het eerst gezien wordt; dat is bewust "best effort" (Meta's render-pagina verandert weleens, downloads kunnen falen). Het dashboard valt dan terug op de Ad Library-link. Alsnog proberen te downloaden voor ads zonder media: `adscout creatives backfill`.

> **Stand van zaken juli 2026:** Meta levert de snapshot-pagina momenteel als lege JavaScript-schil zonder media-URL's in de broncode, waardoor de download voor álle ads niets oplevert. De officiële API geeft voor commerciële ads geen directe media-URL's, en interne endpoints naspelen doen we bewust niet (compliance-grens). Creatives bekijk je dus via de Ad Library-link op elke ad-kaart — alle overige functionaliteit (looptijd, copy, nieuw/gestopt, rapporten) werkt volledig. Tip: draai `adscout collect --skip-creatives` zolang dit zo is (scheelt tijd), en probeer af en toe `adscout creatives backfill` — als Meta het weer server-side rendert, werkt het vanzelf weer. Structureel alternatief: een gelicentieerde ad-intelligence-provider aansluiten via de `AdSource`-adapter.

**"database is locked"**
SQLite laat maar één schrijver tegelijk toe. Dit gebeurt vrijwel alleen als het dashboard openstaat terwijl `adscout collect` draait. Oplossing: dashboard sluiten (Ctrl+C) en het commando **gewoon opnieuw draaien**. Er gaat niets verloren.

**Waar staan de logs?**
- `logs/adscout.log` — alles wat AdScout doet, met detail (roteert vanzelf, wordt nooit oneindig groot).
- `logs/cron.log` — de output van de geplande dagelijkse runs (route a uit sectie 4).
- `adscout status` — de laatste runs uit de `runs`-tabel in de database: wanneer, hoeveel ads, welke fouten.

---

## 10. Onderhoud

### Dependencies veilig updaten

Alle Python-pakketten staan **vastgepind** in `requirements.txt` (en gespiegeld in `pyproject.toml`) — vandaag geïnstalleerd is over twee jaar exact zo herinstalleerbaar. Updaten is zelden nodig (1–2× per jaar is prima) en gaat zo:

1. Verhoog de versienummers ("pins") in `requirements.txt` én in `pyproject.toml`.
2. Installeer en test **vóór** je het gaat gebruiken:

   ```bash
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   pytest
   ```

3. Alleen als alle tests groen zijn: commit de nieuwe pins. Faalt er iets, draai de pins terug en installeer opnieuw.

### Graph API-versie ophogen

Meta zet elke Graph API-versie na ± 2 jaar uit ("deprecation"). Symptoom: API-fouten die over de versie klagen. De versie staat op **één plek**: `META_GRAPH_VERSION` in `.env` (nu `v23.0`). Ophogen = daar bijvoorbeeld `v25.0` invullen en `python verify_access.py` draaien om te checken dat alles nog werkt. Wat er per versie verandert staat in de changelog: https://developers.facebook.com/docs/graph-api/changelog

### Databasemigraties

Schemawijzigingen staan als genummerde SQL-bestanden in `adscout/migrations/` en worden automatisch toegepast zodra AdScout start. Twee regels:

- **Nooit een oud (al toegepast) migratiebestand wijzigen.**
- Iets veranderen aan het schema = een **nieuw** bestand met het volgende nummer toevoegen, bijvoorbeeld `0002_add_kolom.sql`.

### Een nieuwe AdSource-provider aansluiten

Willen we later een betaalde databron (of een andere gratis bron) gebruiken:

1. Maak `adscout/sources/<provider>.py` met een klasse die de `AdSource`-interface implementeert (`adscout/sources/base.py`): twee methodes, `fetch_ads(...)` en `search_pages(...)`, die de data van de provider omzetten naar `AdRecord`/`PageCandidate` (`adscout/models.py`).
2. Registreer de klasse in `adscout/sources/__init__.py` en voeg hem toe aan de `_source()`-keuze in `adscout/cli.py`.
3. Klaar — collector, database, dashboard en rapporten hoeven niet aangepast. `adscout/sources/fixture.py` is een compleet voorbeeld van zo'n adapter in ~50 regels.

---

## 11. Compliance en fair use

- AdScout gebruikt **uitsluitend officiële, publieke transparantiedata**: de Meta Ad Library API en de openbare Ad Library web-UI. Precies dezelfde informatie die iedereen in de browser kan opzoeken.
- We **omzeilen geen toegangsbeperkingen**: geen scraping achter een login, geen trucs rond rate limits (AdScout remt zichzelf juist af), geen gebruik van andermans tokens.
- Gedownloade **creatives (beelden, video's, teksten) zijn auteursrechtelijk eigendom van de adverteerders**. We bewaren ze uitsluitend voor interne concurrentieanalyse. **Nooit herpubliceren** — niet in eigen advertenties, niet op social media, niet in externe presentaties.
- We verzamelen **geen persoonsgegevens** buiten wat de Ad Library zelf publiek toont (paginanamen en geaggregeerde bereik-/targetinginformatie). Er wordt niets over individuele personen opgeslagen.
- Het Meta-token is persoonsgebonden: niet delen buiten het bedrijf, en de repository met data en secrets blijft **privé**.
