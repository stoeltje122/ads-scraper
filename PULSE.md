# Pulse

Interne tool van Cloudplunge: alle klantfeedback — support-mails, reacties op onze Facebook/Instagram-posts en -ads, reviews over onszelf én reviews over concurrenten — automatisch op één plek, met AI-analyse (sentiment, thema, urgentie) en met één harde regel: **alles wat op een bijwerking of gezondheidsklacht kan wijzen, komt bovenaan te staan.**

Deze handleiding is geschreven voor onszelf, twee niet-technische oprichters. Uitgangspunt: **over twee jaar moet je dit nog kunnen draaien zonder hulp**. Lees minimaal secties 1 t/m 3; de rest is naslag per onderwerp.

Pulse is de zustertool van **AdScout** (zie [README.md](README.md)): zelfde map, zelfde `.env`, zelfde concurrentenlijst — eigen database (`data/pulse.db`), eigen commando (`pulse`), eigen dashboard (poort 8010; AdScout gebruikt 8000).

## Inhoudsopgave

1. [Wat is Pulse](#1-wat-is-pulse)
2. [Snelstart: in 10 minuten een gevuld dashboard](#2-snelstart-in-10-minuten-een-gevuld-dashboard)
3. [Dagelijks gebruik](#3-dagelijks-gebruik)
4. [Gmail koppelen (support-mail)](#4-gmail-koppelen-support-mail)
5. [Facebook/Instagram-reacties koppelen](#5-facebookinstagram-reacties-koppelen)
6. [AI-analyse aanzetten (Anthropic-sleutel)](#6-ai-analyse-aanzetten-anthropic-sleutel)
7. [Trustpilot en bol: de geverifieerde waarheid](#7-trustpilot-en-bol-de-geverifieerde-waarheid)
8. [Handmatige import (volwaardig kanaal)](#8-handmatige-import-volwaardig-kanaal)
9. [Automatisch draaien (dagelijkse run)](#9-automatisch-draaien-dagelijkse-run)
10. [Weekrapport en export](#10-weekrapport-en-export)
11. [Privacy in gewone taal (AVG)](#11-privacy-in-gewone-taal-avg)
12. [Backup en herstel](#12-backup-en-herstel)
13. [Troubleshooting](#13-troubleshooting)
14. [Onderhoud en uitbreiden](#14-onderhoud-en-uitbreiden)

---

## 1. Wat is Pulse

Pulse luistert op vier plekken en zet alles in één doorzoekbare database met historie:

| Kanaal | Hoe | Status |
|---|---|---|
| Support-mail (`info@cloudplunge.com`) | Officiële Gmail API, alleen-lezen | automatisch, na eenmalige koppeling (sectie 4) |
| Reacties op eigen Facebook/Instagram-posts en -ads | Officiële Meta Graph API met Page-token | automatisch, na eenmalige koppeling (sectie 5) |
| Eigen reviews (Trustpilot, bol) | Handmatige import — er bestaat geen gratis officiële API (sectie 7) | handmatig, 5 min per maand |
| Concurrent-reviews en -comments | Handmatige import — automatisch verzamelen mag/kan niet legitiem (sectie 7) | handmatig, wanneer jullie willen |

Elk item wordt daarna door de **Claude API** geanalyseerd: sentiment (positief/neutraal/negatief), thema's (bijv. "werking-inslapen", "levering-verzending"), urgentie, type (klacht/vraag/compliment/suggestie/review) en — het allerbelangrijkste — een **gezondheidsflag**. Bij twijfel wordt geflagd. Er is bovendien een trefwoord-vangnet dat al vóór de AI-analyse werkt: een mail met "hartkloppingen" staat direct in het Urgent-scherm, ook als er nog geen AI-sleutel is ingesteld.

> ⚕ **Pulse geeft nooit medisch advies en stelt geen diagnoses.** Het markeert alleen meldingen voor jullie eigen, menselijke opvolging.

**De architectuur in woorden:**

```
bronnen (Gmail API · Meta Graph API · handmatige import · fixtures voor demo)
        │  via de SourceAdapter-interface (pulse/sources/)
        ▼
collector (dagelijkse run: ophalen, dedupliceren, gezondheids-trefwoordcheck,
           retentie-opschoning; één kapotte bron stopt nooit de rest)
        ▼
SQLite-database (data/pulse.db) — items, analyses, threads, runs (audit trail)
        ▼
AI-analyse (pulse analyze, Claude API, alleen nieuwe items, wachtrij)
        ▼
dashboard (pulse web) · weekrapport (pulse report) · export (pulse export)
```

## 2. Snelstart: in 10 minuten een gevuld dashboard

Zonder ook maar één sleutel of wachtwoord zie je Pulse volledig werken op realistische voorbeelddata:

```bash
cd ads-scraper
python3 -m venv .venv                      # eenmalig (al gedaan als AdScout draait)
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt -e .       # eenmalig
cp .env.example .env                       # eenmalig: je instellingenbestand
                                           # (voor de demo niet nodig, wel voor secties 4-6)

pulse init      # database + bronnen + thema's + concurrentenlijst
pulse demo      # voorbeelddata: mails, reacties, reviews, concurrent-reviews + analyse
pulse web       # dashboard op http://localhost:8010
```

Open <http://localhost:8010>. Je landt op **Urgent**: bovenaan staan de (voorbeeld)meldingen met een mogelijk gezondheidssignaal, rood gemarkeerd. Klik rond in Inbox, Trends, Kansen, Import en Beheer — alles is echt en klikbaar. Stoppen: `Ctrl+C` in de terminal.

`pulse demo` is veilig: het raakt geen echte accounts en je kunt het zo vaak draaien als je wilt (dubbel draaien maakt geen duplicaten). Wil je later met een schone, echte database beginnen: verwijder `data/pulse.db*` en draai `pulse init` opnieuw.

## 3. Dagelijks gebruik

De dagelijkse routine is: **run draait 's nachts automatisch** (sectie 9) → jullie kijken 's ochtends 2 minuten naar het dashboard.

- **Urgent** — het startscherm zodra er iets openstaat. Gezondheidssignalen eerst (rood), daarna overige urgente zaken (bijv. een klant die met geld-terug dreigt). Afgehandeld? Klik **"Markeer als opgevolgd"** — zo zien jullie allebei wat al gedaan is. Concurrent-items komen hier bewust nooit tussen: een bijwerking bij een ander merk is een inzicht (zie Kansen), niet een supportcase van ons.
- **Inbox** — alles, filterbaar op bron, sentiment, thema, type, urgentie, eigen merk/concurrent en periode, met zoekveld. Klik een item voor de volledige tekst, de analyse en (bij mails) de hele conversatie.
- **Trends** — volume per week, sentiment per week, top-klachten en top-complimenten deze week vs vorige week, en de thema-verdeling. Elke balk is klikbaar en opent de Inbox met dat filter.
- **Kansen** — per concurrent de terugkerende klachten en pluspunten uit hun reviews, met de vraag die telt: *welke advertentie-invalshoek volgt hieruit voor Cloudplunge?* Gebruik dit naast AdScout: AdScout laat zien wát ze adverteren, Pulse laat zien waar hun klanten over klagen.
- **Import** — het plak-vak en de CSV-upload (sectie 8).
- **Beheer** — bronnen aan/uit, concurrenten en hun review-URL's, thema's, retentie-instelling, "iemand vergeten" (AVG), analyse-wachtrij en de laatste runs.

Vanaf de terminal is alles er ook: `pulse status` (gezondheidscheck van het hele systeem), `pulse sources`, `pulse collect`, `pulse analyze`, `pulse report --weekly`, `pulse export --csv`, `pulse --help` voor de rest.

## 4. Gmail koppelen (support-mail)

Pulse leest de inbox van `info@cloudplunge.com` **alleen-lezen** via de officiële Gmail API. Nieuwsbrieven, notificaties en onze eigen antwoorden worden automatisch weggefilterd; de eerste keer kijkt hij 90 dagen terug, daarna alleen wat nieuw is.

Eenmalig, circa 15 minuten. Je hebt nodig: de Google-inlog van het `info@cloudplunge.com`-account.

**Stap A — Google Cloud-project aanmaken**
1. Ga naar <https://console.cloud.google.com/> en log in met het Cloudplunge-account.
2. Klik bovenin op de projectkiezer → **New project**. Naam: `pulse-cloudplunge`. Klik **Create** en wacht tot het project actief is (belletje rechtsboven).

**Stap B — Gmail API aanzetten**
1. Menu (☰) → **APIs & Services** → **Library**.
2. Zoek "Gmail API" → klik erop → **Enable**.

**Stap C — Toestemmingsscherm (consent screen)**
1. **APIs & Services** → **OAuth consent screen**.
2. Kies bij "Audience" voor **External** (tenzij jullie Google Workspace gebruiken; kies dan **Internal** en sla stap 5 over) en klik **Create**.
3. App name: `Pulse`. Support-mail en developer-mail: `info@cloudplunge.com`. De rest mag leeg. **Save and continue** tot je er doorheen bent (scopes hoef je hier niet toe te voegen).
4. Onder **Audience → Test users**: klik **Add users** en voeg `info@cloudplunge.com` toe. *(Dit is belangrijk: zonder dit weigert Google straks de inlog met "access_denied".)*
5. Zet de app daarna meteen op **"In production"** (knop op dezelfde pagina onder "Publishing status"): in "Testing"-modus laat Google de login elke 7 dagen verlopen en zou je wekelijks opnieuw moeten inloggen. Google toont dan een waarschuwing over app-verificatie — die is bedoeld voor apps met publiek, en mag je voor deze eigen interne app negeren (bij de login klik je straks op "Continue/Doorgaan").

**Stap D — Credentials downloaden**
1. **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**.
2. Application type: **Desktop app**. Naam: `pulse-desktop`. Klik **Create**.
3. Klik **Download JSON**. Er downloadt een bestand `client_secret_….json`.
4. Maak in de projectmap de map `data/gmail/` aan en zet het bestand daar neer met de naam **`credentials.json`**:
   ```bash
   mkdir -p data/gmail
   mv ~/Downloads/client_secret_*.json data/gmail/credentials.json
   ```

**Stap E — Eenmalig inloggen en aanzetten**
```bash
pip install -r requirements-gmail.txt   # eenmalig: de Google-pakketten
pulse verify gmail                      # er opent een browservenster
```
Log in met `info@cloudplunge.com`, klik bij de waarschuwing "Google hasn't verified this app" op **Continue** (het is onze eigen app), en geef alleen-lezen-toegang. Pulse schrijft dan `data/gmail/token.json` en meldt: *"Gmail gekoppeld: info@cloudplunge.com"*. Daarna:
```bash
pulse source activate gmail
pulse collect
```
De eerste run haalt tot 90 dagen mail op (instelbaar via `PULSE_MAIL_BACKFILL_DAYS` in `.env`).

> 🔐 `data/gmail/credentials.json` en `token.json` zijn sleutels tot de mailbox (alleen-lezen). Ze staan in `data/` en komen dus nooit in git; behandel backups ervan even zorgvuldig als een wachtwoord.

## 5. Facebook/Instagram-reacties koppelen

Pulse haalt reacties op onze eigen posts én ad-posts op via de officiële Graph API. Daarvoor is een **Page-token** nodig van de Cloudplunge-pagina. We hergebruiken de Meta-app die al voor AdScout bestaat (README sectie 3).

1. Ga naar <https://developers.facebook.com/tools/explorer/> en kies rechtsboven de AdScout/Cloudplunge-app.
2. Klik bij "User or Page" op **Get User Access Token** en vink de permissies aan: `pages_show_list`, `pages_read_engagement`, `pages_read_user_content`. Klik **Generate Access Token** en doorloop de login.
3. Kies daarna bij "User or Page" de **Cloudplunge-pagina** — het token in het veld is nu een *Page*-token.
4. Page-tokens die je zo genereert verlopen na ±1-2 uur. Maak er een langlevend token van: plak het token in de **Access Token Debugger** (<https://developers.facebook.com/tools/debug/accesstoken/>) → knop **Extend Access Token**. Een verlengd *Page*-token van een klassieke pagina verloopt daarna in de praktijk niet.
5. Zet in `.env`:
   ```
   META_PAGE_TOKEN=<het lange token>
   PULSE_META_PAGE_ID=<het paginanummer>
   ```
   Het paginanummer vind je in de Graph API Explorer (query `me?fields=id,name` met het Page-token) of op de Facebook-pagina onder "About".
6. Controleer en zet aan:
   ```bash
   pulse verify meta_comments     # "Pagina gekoppeld: Cloudplunge"
   pulse source activate meta_comments
   ```

Wat Pulse ophaalt: reacties van klanten op onze posts en (waar de API dat toestaat) op onze advertentie-posts. Onze eigen antwoorden worden overgeslagen. **Reacties op posts van concurrenten kan en mag niet via de API** — dat is bewust handmatige import (sectie 7/8).

> Verloopt het token ooit (foutmelding "fout 190" in `pulse status`), herhaal dan stappen 1-5. Dat is 5 minuten werk.

## 6. AI-analyse aanzetten (Anthropic-sleutel)

Zonder sleutel werkt alles behalve de analyse: verzamelen gaat door en items wachten netjes in de wachtrij (het trefwoord-vangnet voor gezondheidssignalen werkt óók zonder sleutel). Met sleutel:

1. Ga naar <https://console.anthropic.com/>, maak een account (of log in — dezelfde sleutel als AdScout gebruikt mag).
2. Ga naar **API Keys** → **Create key**, naam `pulse`, kopieer de sleutel (begint met `sk-ant-`).
3. Zet in `.env`: `ANTHROPIC_API_KEY=sk-ant-...`
4. Eenmalig: `pip install -r requirements-ai.txt`
5. Draai: `pulse analyze`

> 💶 **Dit kost een klein bedrag per gebruik.** Pulse gebruikt standaard een kostenefficiënt model (Claude Haiku); reken op minder dan een eurocent per feedback-item, dus hooguit enkele euro's per maand bij normale volumes. In de Anthropic-console kun je een maandlimiet instellen (Settings → Limits) — doe dat, bijvoorbeeld $10. Alleen díe analyse-stap stuurt tekst naar Anthropic; zie sectie 11.

Het model is instelbaar via `PULSE_ANTHROPIC_MODEL` in `.env`. Mislukt de analyse van een item (bijv. een onleesbaar antwoord), dan wordt het gemarkeerd voor heranalyse en bij de volgende `pulse analyze` opnieuw geprobeerd — nooit stilletjes overgeslagen (`pulse status` en Beheer laten zien of dat speelt).

## 7. Trustpilot en bol: de geverifieerde waarheid

Dit is in juli 2026 uitgezocht (bronnen onderaan deze sectie), zodat jullie niet hoeven te gissen:

**Trustpilot.** Trustpilot heeft een officiële API waarmee een bedrijf zijn eigen reviews automatisch kan ophalen, maar die zit **niet** in het gratis businessaccount: je hebt er een betaald abonnement met API-module of een Enterprise-contract voor nodig (reken op duizenden euro's per jaar, prijs op offerte). Reviews van concurrenten ophalen kan officieel alleen via Trustpilots betaalde "Data Solutions"-product. Zelf reviewpagina's scrapen is uitdrukkelijk verboden in Trustpilots voorwaarden — ook de "Trustpilot-API's" van derde partijen zijn in feite scrapers en dus geen optie. **Onze route: handmatige import** (5 minuten per maand: reviews kopiëren en in het plak-vak zetten). Mocht er ooit een betaald plan met API-module komen, dan staat de adapter-plek klaar (`pulse/sources/trustpilot.py`).

**bol.** De officiële bol Retailer API (gratis met ons verkopersaccount) geeft **geen geschreven productreviews** terug — geen tekst, naam of datum, ook niet via het verkoopdashboard. Wat wél officieel kan: per EAN de **sterrenverdeling** van elk product opvragen, ook van concurrentproducten (bol noemt concurrentie-analyse zelf als toegestaan gebruik). Reviewteksten scrapen van productpagina's is tegen bol's voorwaarden. **Onze route: handmatige import voor reviewteksten**; automatische sterren-tracking per EAN is een mogelijke latere uitbreiding (genoteerd in OCHTEND.md).

Beide bronnen staan daarom in Pulse als "gepauzeerd" met de reden erbij (`pulse sources` laat het zien). Handmatig geïmporteerde Trustpilot/bol-reviews worden gewoon onder die kanalen geregistreerd, zodat de filters kloppen.

<details>
<summary>Geraadpleegde bronnen (juli 2026)</summary>

- Trustpilot API-documentatie en plannen: developers.trustpilot.com (Business Units/Service Reviews/Product Reviews API, "API module best practices", Data Solutions API), business.trustpilot.com/pricing
- Trustpilot anti-scraping: corporate.trustpilot.com/legal ("Action We Take", maart 2026) en de Terms of Use
- bol Retailer API v10: api.bol.com/retailer/public — Insights → "Get product ratings" (sterrenverdeling per EAN); geen reviews-endpoint
- bol Marketing Catalog API: alleen rating-samenvattingen, geen reviewtekst
</details>

## 8. Handmatige import (volwaardig kanaal)

Handmatige import is geen noodoplossing maar een eersteklas kanaal: alles wat je importeert gaat door **dezelfde** deduplicatie en AI-analyse als automatisch verzamelde items. Drie manieren:

**a) Plak-vak (dashboard → Import).** Plak één of meer reviews (meerdere scheiden met een regel `---`), kies het kanaal (Trustpilot/bol/social/e-mail/overig), kies over wie het gaat (Cloudplunge of een concurrent) en eventueel de datum. Klaar.

**b) CSV-upload (zelfde pagina) of `pulse import bestand.csv`.** Handig voor grotere ladingen, bijv. via Excel. Alleen een tekstkolom is verplicht; herkende koppen (Nederlands of Engels, volgorde vrij):

```csv
kanaal,concurrent,datum,sterren,titel,tekst,auteur,url
trustpilot,Cloudpillo,2026-06-27,2,Kussen zakt in,"Na twee maanden al ingezakt.",Renate B.,https://…
bol,,2026-06-28,5,,"Heerlijk product, slaap super.",M. de Vries,
```
Een voorbeeldbestand staat in `tests/fixtures/pulse/competitor_reviews.csv`.

**c) `pulse import bestand.txt`** — tekstbestand in het plak-formaat (blokken gescheiden door `---`).

Dubbel importeren kan geen kwaad: Pulse herkent identieke items (zelfde tekst + auteur + datum) en slaat ze over. Onbekende concurrentnamen worden automatisch aan de watchlist toegevoegd.

**Concurrenten-watchlist.** De negen concurrenten (Cloudpillo, Zelesta, 8hours, Cabau, Evidaplus, Doré & Rosé, Ella, Elvou, Oana) staan er na `pulse init` al in, gedeeld met AdScout (`competitors.seed.yaml`). Per concurrent kun je onder Beheer de review-URL's invullen (Trustpilot-/bol-pagina's) — puur als geheugensteun bij het maandelijkse import-rondje. Beheren kan ook via `pulse competitor add/list/pause/set-urls`.

## 9. Automatisch draaien (dagelijkse run)

De dagelijkse run is: `pulse collect` (ophalen + opschonen) en daarna `pulse analyze` (als er een AI-sleutel is). Kies één van de drie routes; de bestanden staan klaar in `ops/`.

**macOS (aanbevolen als Pulse op een Mac draait) — launchd:**
```bash
sed "s#/REPLACE/PAD/NAAR/ads-scraper#$(pwd)#g" ops/nl.cloudplunge.pulse.plist \
  > ~/Library/LaunchAgents/nl.cloudplunge.pulse.plist
launchctl load ~/Library/LaunchAgents/nl.cloudplunge.pulse.plist
```
Draait elke ochtend om 07:30 (en op maandag ook het weekrapport). Logs: `logs/cron.log`.

**Linux/server — cron:**
```bash
crontab -e
# en voeg toe:
30 7 * * * /pad/naar/ads-scraper/ops/pulse-cron.sh
```

**GitHub Actions (geen eigen computer nodig)** — workflow staat in `.github/workflows/pulse-collect.yml`, standaard **uitgeschakeld**. Deze route vereist een **privérepo** (de database met klantfeedback wordt naar de repo gecommit) en een eenmalige lokale bootstrap: Gmail koppelen, één `pulse collect`, en dan alleen de database committen (`git add -f data/pulse.db`). Het Gmail-token gaat níet in git maar als secret `GMAIL_TOKEN_JSON` (de inhoud van `data/gmail/token.json`), samen met `META_PAGE_TOKEN`, `PULSE_META_PAGE_ID` en optioneel `ANTHROPIC_API_KEY`. Daarna de `schedule`-regels ont-commentariëren — de volledige stap-voor-stap staat in de kop van het workflow-bestand.

## 10. Weekrapport en export

`pulse report --weekly` schrijft `reports/pulse-weekrapport-<datum>.md` en `.html`: aantallen per bron, urgente zaken (en of ze zijn opgevolgd), top-3 klachten en complimenten vs vorige week, de opvallendste trend, concurrent-kansen en een korte lijst aanbevolen acties in gewone taal. Oude rapporten blijven staan en zijn ook te openen via dashboard → Beheer → Weekrapporten.

Rapporten worden nu alleen lokaal opgeslagen; er is een `Notifier`-interface (`pulse/notify.py`) voorbereid zodat "mail het rapport" of "post in Slack" er later in een middag bij kan zonder iets anders te verbouwen.

`pulse export --csv` / `--json` exporteert álles (items + analyses) naar `exports/` — geen lock-in, handig voor eigen draaitabellen.

## 11. Privacy in gewone taal (AVG)

- **Alles staat lokaal** op onze eigen computer: de database (`data/pulse.db`), rapporten en logs. Er is geen cloud-dienst van derden waar de feedback heen gaat, met één uitzondering: bij `pulse analyze` gaat de **tekst van het feedback-item** naar de Claude API van Anthropic om geclassificeerd te worden. Pulse stuurt daarbij geen aparte naam-, adres- of e-mailvelden mee — maar let op: de tekst zélf kan natuurlijk een naam bevatten (bijv. een ondertekening onder een mail).
- **Dataminimalisatie:** Pulse bewaart alleen wat nodig is voor feedbackanalyse: de tekst, datum, bron en een weergavenaam. Adresgegevens, betaalinformatie of bijlagen uit mails worden nooit opgeslagen.
- **Afzenders worden gehasht:** het e-mailadres of profiel-ID wordt direct omgezet in een onherkenbare code (hash) en het kale adres wordt nergens als sleutel bewaard.
- **Iemand vergeten:** `pulse forget naam@voorbeeld.nl` (of dashboard → Beheer → "Iemand vergeten") verwijdert álle items, analyses en bijbehorende mail-conversaties van die persoon uit de database, definitief. Vraagt iemand om verwijdering van zijn gegevens, dan is dit de knop. Eén ding doet de knop niet: al eerder gegenereerde weekrapport-bestanden (in `reports/`) kunnen een citaat bevatten — verwijder in dat geval ook het betreffende bestand.
- **Retentie:** items ouder dan 24 maanden (instelbaar onder Beheer) worden automatisch opgeschoond bij de dagelijkse run.
- **Gmail is alleen-lezen:** Pulse kan niets versturen, beantwoorden of verwijderen in de mailbox.

## 12. Backup en herstel

Backup = kopie van de map `data/` (database + Gmail-tokens) en eventueel `reports/`. Zet die kopie ergens veilig (externe schijf of versleutelde cloudmap). Herstel = bestanden terugzetten en `pulse status` draaien. De database-structuur wordt automatisch bijgewerkt door migraties; een oude backup openen met een nieuwe versie van Pulse is dus veilig.

## 13. Troubleshooting

| Melding / symptoom | Oorzaak & oplossing |
|---|---|
| `Lege database in … — is dit de juiste map?` | Je staat niet in de projectmap, of `pulse init` is nooit gedraaid. `cd ads-scraper` en/of `pulse init`. |
| `Geen Google-credentials gevonden` | `data/gmail/credentials.json` ontbreekt — sectie 4, stap D. |
| Browser zegt **"access_denied"** bij Gmail-login | Het account is niet toegevoegd als test user — sectie 4, stap C4. |
| `Gmail is nog niet ingelogd` | Draai eenmalig `pulse verify gmail` (opent de browser). |
| `Meta Page-token verlopen of ongeldig (fout 190)` | Nieuw token maken — sectie 5, het is 5 minuten werk. |
| `Geen ANTHROPIC_API_KEY in .env — analyse staat uit` | Geen probleem: verzamelen gaat door. Sleutel instellen: sectie 6. |
| `X item(s) wachten op heranalyse` in `pulse status` | Een analyse-antwoord was onleesbaar. Gewoon nog eens `pulse analyze`; blijft het hangen, kijk in Beheer welk item het is. |
| `Geen tekstkolom gevonden` bij CSV-import | De CSV mist een kolom die 'tekst' of 'review' heet — zie sectie 8b. |
| Dashboard doet niks / poort bezet | Er draait al een `pulse web` (of AdScout op dezelfde poort). Stop met `Ctrl+C` of kies `pulse web --port 8020`. |
| Iets anders geks | Kijk in `logs/pulse.log` (de laatste regels zeggen meestal precies wat er misging) en in `pulse status`. |

## 14. Onderhoud en uitbreiden

- **Dependencies veilig updaten.** Versies staan vastgepind in `requirements*.txt`. Updaten doe je bewust: één regel verhogen, `pip install -r requirements.txt`, daarna de testsuite draaien (eenmalig `pip install -r requirements-dev.txt`, dan `python -m pytest` — alles hoort groen te zijn) en `pulse demo` + `pulse web` als rooktest. Werkt iets niet: versie terugdraaien.
- **Database-migraties.** Schemawijzigingen gaan uitsluitend via nieuwe, genummerde bestanden in `pulse/migrations/` — nooit een bestaand migratiebestand aanpassen. Pulse voert nieuwe migraties automatisch en veilig uit bij de eerstvolgende start.
- **Thema's aanpassen** (bijv. een nieuw terugkerend onderwerp): dashboard → Beheer → Thema's, of `pulse theme add`. De AI gebruikt het nieuwe thema vanaf de eerstvolgende analyse; code aanpassen is niet nodig.
- **Bron aan/uit:** Beheer of `pulse source pause/resume/activate` — zonder code.
- **Een écht nieuw automatisch kanaal toevoegen** (bijv. ooit de betaalde Trustpilot-API): dat is het enige waar een klein beetje code bij komt kijken. Het recept staat in `pulse/sources/base.py`: één adapterbestand met `collect()` en `verify()`, één regel registreren in `pulse/sources/__init__.py`, één regel in `store.STANDARD_SOURCES`. Collector, database, dashboard en rapporten blijven ongewijzigd. Tot die tijd kan elk nieuw kanaal vandaag al mee via handmatige import.
- **Logs**: console én `logs/pulse.log` (roteert vanzelf, max ~25 MB). Elke run staat bovendien in de runs-tabel (dashboard → Beheer).
