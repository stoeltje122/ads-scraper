# Compass

Interne tool van Cloudplunge: elke dag automatisch bijhouden wat we verkopen (Shopify + bol), wat we aan advertenties uitgeven (Meta), wat daarvan overblijft (marge) en hoe onze voorraad ervoor staat. Compass beantwoordt drie vragen: **verdienen we aan onze advertenties, kunnen we opschalen, en wanneer moeten we voorraad bestellen?**

Deze README is geschreven voor onszelf, twee niet-technische oprichters. Uitgangspunt: **over twee jaar moet je dit nog kunnen draaien zonder hulp**. Lees minimaal secties 1 t/m 6; de rest is naslag.

## Inhoudsopgave

1. [Wat is Compass](#1-wat-is-compass)
2. [Snelstart](#2-snelstart)
3. [Shopify custom app aanmaken](#3-shopify-custom-app-aanmaken)
4. [Meta Marketing API koppelen](#4-meta-marketing-api-koppelen)
5. [bol Retailer API koppelen](#5-bol-retailer-api-koppelen)
6. [Kostenmodel invullen](#6-kostenmodel-invullen)
7. [Dagelijkse run instellen](#7-dagelijkse-run-instellen)
8. [CSV-import](#8-csv-import)
9. [Weekrapport en export](#9-weekrapport-en-export)
10. [Backup en herstel](#10-backup-en-herstel)
11. [Signalen uitgelegd](#11-signalen-uitgelegd)
12. [Troubleshooting](#12-troubleshooting)
13. [Onderhoud](#13-onderhoud)
14. [Compliance en privacy](#14-compliance-en-privacy)

---

## 1. Wat is Compass

Compass haalt elke dag via de officiële API's onze eigen cijfers op — orders uit **Shopify** en **bol**, advertentie-uitgaven uit ons eigen **Meta-advertentieaccount**, en de voorraadstand — en legt daar ons eigen **kostenmodel** overheen (inkoopprijs, verzendkosten, betaalkosten, bol-commissie, vaste lasten). Het resultaat bekijk je in een lokaal dashboard, een wekelijks rapport en exports. Compass is gebouwd rond ons ene product: het zakje met 60 capsules à € 29,95.

**De architectuur in woorden:**

```
bronnen (Shopify Admin API · Meta Insights API · bol Retailer API,
         of CSV/demo-data voor offline gebruik)
        │  via alleen-lezen adapters (OrdersSource / AdSpendSource / InventorySource)
        ▼
collector (dagelijkse run: orders, ad spend en voorraad ophalen)
        ▼
SQLite-database (data/compass.db) + kostenmodel (jullie eigen invoer)
        ▼
dashboard (compass web) · signalen · weekrapport (compass report) · export (compass export)
```

Alles draait lokaal op je eigen computer (of optioneel via GitHub Actions, zie [sectie 7](#7-dagelijkse-run-instellen)). Er is geen server, geen abonnement en geen cloud-database. Compass deelt de map en de `.env` met AdScout, maar heeft zijn eigen database en zijn eigen dashboard.

### De zeven dashboardpagina's

| Pagina | Wat je er ziet |
|---|---|
| **Vandaag** | omzet, orders, spend en marge van vandaag en deze week; MER vs break-even ROAS als groen/oranje/rood-oordeel; "volgens Meta vs werkelijk"; openstaande signalen |
| **Maand** | per maand: omzet per kanaal, kostenopbouw (inkoop, verzending, fees, bol-commissie), marge, vaste lasten en een netto-indicatie |
| **Advertenties** | per campagne: spend, impressies, kliks, aankopen en ROAS "volgens Meta" — met daaronder het eerlijke verschil met de werkelijke omzet |
| **Economie** | de opbouw van één gemiddelde order per kanaal (van omzet naar marge) en wat jullie break-even ROAS betekent |
| **Voorraad** | voorraadstand, verkoopsnelheid, bestelpunt en verwachte uitverkoopdatum; hier voer je ook een handmatige telling in |
| **Signalen** | actieve adviezen met de cijfers erachter, plus de historie |
| **Instellingen** | kostenmodel (met versiegeschiedenis), signaaldrempels en de status van de drie bronnen |

### De begrippen in één tabel

| Begrip | Betekenis |
|---|---|
| **Omzet excl. btw** | omzet incl. btw gedeeld door 1,09 (bij 9% btw) |
| **Contributiemarge** | omzet excl. btw − inkoop − verzending − betaalkosten − bol-commissie: wat één order echt bijdraagt |
| **Break-even ROAS** | 1 / marge-ratio: elke euro ad spend moet minstens zóveel omzet (excl. btw) opleveren om quitte te spelen |
| **MER** | omzet excl. btw / ad spend, over álle verkoop samen ("blended") — onze echte advertentie-efficiëntie |
| **"volgens Meta"** | Meta's eigen claim over aankopen en waarde (attributie). Staat altijd apart, náást de werkelijke cijfers — het is nooit "de omzet" |
| **CAC** | ad spend / nieuwe klanten: wat één nieuwe klant kost |
| **AOV** | omzet / aantal orders: gemiddelde orderwaarde |

### Wat Compass bewust NIET doet

- **Geen bankkoppeling.** Compass kijkt naar orders en ad spend, niet naar jullie bankrekening.
- **Geen boekhouding.** Alle winst- en margecijfers zijn *indicatief*, op basis van het kostenmodel dat jullie zelf invullen. De boekhouder blijft leidend.
- **Compass wijzigt nooit iets.** Geen campagne pauzeren, geen budget verhogen, geen prijs aanpassen, geen order aanraken — alle koppelingen zijn alleen-lezen. Compass geeft signalen met de cijfers erbij; **jullie beslissen**.

> Dit zijn vuistregels op basis van jullie eigen cijfers, geen financieel advies — jullie beslissen.

---

## 2. Snelstart

### Stap 1 — Python 3.11 of nieuwer installeren

Precies zoals bij AdScout: download de installer op [python.org/downloads](https://www.python.org/downloads/) (Windows: vink **"Add python.exe to PATH"** aan). Check in een terminal:

```bash
python3 --version    # Windows: python --version
```

### Stap 2 — Compass installeren

Compass zit in hetzelfde project als AdScout. **Draait AdScout al op deze computer? Dan is Compass al geïnstalleerd** — mogelijk moet je één keer `pip install -e .` opnieuw draaien in de venv, en ga daarna door naar stap 3. Anders, open een terminal in de projectmap (`ads-scraper`):

```bash
python3 -m venv .venv                # eenmalig: geïsoleerde Python-omgeving
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env                 # Windows: copy .env.example .env
```

> Elke keer dat je een nieuwe terminal opent, activeer je eerst weer de venv met `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`). Daarna werkt het commando `compass`.

### Stap 3 — Eerst proefdraaien op demo-data (geen accounts nodig)

```bash
compass demo                  # bouwt 90 dagen realistische voorbeelddata
compass web --demo            # dashboard op http://127.0.0.1:8010 (stoppen: Ctrl+C)
```

De demo gebruikt een **aparte database** (`data/compass-demo.db`), dus voorbeelddata kan nooit tussen je echte orders belanden. Klik alle zeven pagina's even door — dan snap je in vijf minuten wat Compass voor je doet, inclusief een voorraadsignaal en het verschil tussen "volgens Meta" en werkelijk.

### Stap 4 — Echte data

```bash
compass init                  # database aanmaken + placeholder-kostenmodel
compass verify                # per bron: werkt de koppeling, of wat is er nog nodig?
```

`compass verify` vertelt je per bron precies wat er ontbreekt en laat intussen op voorbeelddata zien wat je gaat krijgen. Daarna:

1. Credentials instellen: [sectie 3](#3-shopify-custom-app-aanmaken) (Shopify), [sectie 4](#4-meta-marketing-api-koppelen) (Meta) en [sectie 5](#5-bol-retailer-api-koppelen) (bol).
2. **Kostenmodel invullen** — [sectie 6](#6-kostenmodel-invullen). Zonder echte kosten kloppen de marges niet.
3. Historie ophalen en het dashboard starten:

```bash
compass backfill --days 365   # eenmalig: een jaar historie ophalen
compass collect               # de dagelijkse run (zie sectie 7 om te automatiseren)
compass web                   # dashboard op http://127.0.0.1:8010
```

> Het dashboard draait op poort **8010**, zodat AdScout (`adscout web`, poort 8000) tegelijk open kan staan.

### Alle commando's op een rij

| Commando | Wat het doet |
|---|---|
| `compass init` | database aanmaken + placeholder-kostenmodel seeden |
| `compass demo` | demo-database bouwen (90 dagen voorbeelddata), daarna `compass web --demo` |
| `compass verify [bron]` | koppeling controleren (`shopify`, `meta` of `bol`; zonder argument: alle drie) |
| `compass collect` | dagelijkse run: orders, ad spend en voorraad ophalen + signalen evalueren |
| `compass backfill --days 365` | zelfde als collect, maar dan met een lange periode historie |
| `compass costs show` / `compass costs set` | kostenmodel bekijken / een nieuwe versie invullen |
| `compass report` | weekrapport schrijven naar `reports/` |
| `compass status` | gezondheid: bronnen, laatste runs, databereik, kostenmodel, signalen |
| `compass import bestand.csv` | CSV-import van orders, spend of voorraad ([sectie 8](#8-csv-import)) |
| `compass export --csv` | alle data exporteren, zonder lock-in ([sectie 9](#9-weekrapport-en-export)) |
| `compass web` | dashboard op http://127.0.0.1:8010 |

---

## 3. Shopify custom app aanmaken

Eenmalig, ±10 minuten. Je maakt in je eigen Shopify-admin een "custom app" die alleen mág lezen, en kopieert het bijbehorende token naar `.env`.

1. **Log in op de Shopify-admin** van de winkel ([admin.shopify.com](https://admin.shopify.com)).
2. Ga naar **Instellingen → Apps en verkoopkanalen → Apps ontwikkelen** (Engels: Settings → Apps and sales channels → Develop apps; menu kan iets anders heten). De eerste keer vraagt Shopify je om app-ontwikkeling toe te staan — bevestig dat.
3. Klik **"App maken"** (Create an app) en noem hem `Compass`.
4. Open in de nieuwe app het tabblad **Configuratie** (Configuration) en klik bij **Admin API-integratie** op configureren. Vink precies deze drie scopes aan:
   - `read_orders` — orders lezen (verplicht);
   - `read_products` — producten lezen (voor de voorraadstand);
   - `read_inventory` — voorraad lezen (voor de voorraadstand).

   Bewust géén `write_`-scopes: Compass kan dan technisch niets aanpassen, wat er ook gebeurt.
5. Klik **"App installeren"** (Install app) en bevestig.
6. Op het tabblad **API-referenties** (API credentials) staat nu het **Admin API-toegangstoken**. Klik "Token eenmalig weergeven" (Reveal token once) en **kopieer het meteen** — Shopify toont het maar één keer. Het begint met `shpat_`.
7. Zet beide waarden in `.env` in de projectmap:

   ```
   SHOPIFY_SHOP=jouwwinkel.myshopify.com
   SHOPIFY_ACCESS_TOKEN=shpat_...
   ```

   Let op: `SHOPIFY_SHOP` is het **myshopify.com-adres** van de winkel (te zien in de browserbalk van de admin), niet je eigen domeinnaam. `.env` staat in `.gitignore` en komt dus nooit in git terecht.
8. **Controleren:**

   ```bash
   compass verify shopify
   ```

   Dit toont de winkelnaam, het aantal orders van de laatste 30 dagen en of de voorraadstand leesbaar is.

> **Token kwijt of gelekt?** In de admin bij de app onder API-referenties kun je het token intrekken en een nieuwe genereren (menu kan iets anders heten). Oude token weggooien, nieuwe in `.env`.

Twee praktische notities:

- Ontbreken `read_products`/`read_inventory`, dan werkt al het financiële gewoon; alleen de automatische voorraadstand valt weg. Voorraad handmatig bijhouden kan altijd via het dashboard (**Voorraad**) of CSV.
- Geeft een lange backfill maar ±60 dagen orders terug? Shopify beperkt oudere orders tot apps met de extra scope `read_all_orders` (menu kan iets anders heten; soms moet je die toegang bij Shopify aanvragen). Scope aanvinken, app opnieuw installeren en `compass backfill` nogmaals draaien.

---

## 4. Meta Marketing API koppelen

Goed nieuws: we hebben al een Meta developer-app voor AdScout, en Compass **hergebruikt die app én hetzelfde token** (`META_ACCESS_TOKEN` in `.env`). Er komen twee dingen bij: de *ads_read*-permissie op het token, en het ID van ons advertentieaccount.

1. **Zelfde app openen.** Ga naar [developers.facebook.com](https://developers.facebook.com/) → **My Apps** en open de bestaande AdScout-app.
2. **Marketing API toevoegen.** In het app-dashboard: **Add product** (of "Producten toevoegen") → **Marketing API** → Set up (menu kan iets anders heten). Er is verder niets te configureren.
3. **Token met ads_read maken.** Ga naar **Tools → [Graph API Explorer](https://developers.facebook.com/tools/explorer/)**, kies rechtsboven de app, klik **"Generate Access Token"** en vink bij de permissies **`ads_read`** aan. Log in met het Facebook-account dat toegang heeft tot ons advertentieaccount. Voor AdScout waren geen permissies nodig — voor Compass dus wél deze ene, alleen-lezen permissie.
4. **Long-lived maken (±60 dagen geldig).** Precies dezelfde omwisseltruc als bij AdScout — volg de stappen in de [root-README, sectie "Meta developer-app en token"](../README.md#3-meta-developer-app-en-token) (het curl-commando onder stap 6 en "Token vernieuwen").
5. **Token in `.env` zetten** — in dezelfde `META_ACCESS_TOKEN`-regel die er al staat. Eén token voor beide tools: vernieuw je hem, dan zijn AdScout én Compass in één keer geholpen.
6. **Advertentieaccount-ID vinden.** Open [adsmanager.facebook.com](https://adsmanager.facebook.com/). Het account-ID staat in de URL (`act=1234567890`) en in de accountkiezer linksboven (menu kan iets anders heten). Zet het in `.env`:

   ```
   META_AD_ACCOUNT_ID=act_1234567890
   ```

   Met of zonder het voorvoegsel `act_` — Compass maakt het zelf in orde.
7. **Controleren:**

   ```bash
   compass verify meta
   ```

   Dit toont de accountnaam, de valuta (moet **EUR** zijn!), de spend van de laatste 7 dagen en of Meta aankopen doorgeeft ("attributie werkt").

> **Belangrijk:** het token verloopt na **±60 dagen** — zie de root-README, "Token vernieuwen". Zet de bestaande agenda-herinnering van AdScout ook voor Compass in; het is letterlijk hetzelfde token.

---

## 5. bol Retailer API koppelen

Eenmalig, ±5 minuten, in het bol-partnerplatform. Doorlooptijd: meestal direct.

1. **Log in op [partner.bol.com](https://partner.bol.com/)** met het verkoopaccount.
2. Ga naar **Instellingen → Diensten → API-instellingen** (menu kan iets anders heten; zoek anders op "API" in de instellingen).
3. Kies **client credentials aanmaken** (soms "Nieuwe credentials" of "API-sleutel aanmaken"). Je krijgt een **Client ID** en een **Client Secret**. Het secret is — net als bij Shopify — maar één keer zichtbaar: **kopieer het meteen**.
4. Zet beide in `.env`:

   ```
   BOL_CLIENT_ID=...
   BOL_CLIENT_SECRET=...
   ```

5. **Controleren:**

   ```bash
   compass verify bol
   ```

   Dit haalt een token op en toont een voorbeeldorder, zodat je ziet dat de koppeling echt werkt.

Compass gebruikt van de Retailer API uitsluitend de lees-endpoints voor orders. bol geeft geen klantidentiteit door (orders zijn geanonimiseerd); elke bol-order telt daarom als nieuwe klant in de CAC-berekening.

---

## 6. Kostenmodel invullen

Dit is **de belangrijkste invuloefening van Compass**. Elke marge, elk break-even-getal en elk signaal rekent met deze cijfers. `compass init` zet een PLACEHOLDER-model neer zodat alles technisch werkt, maar **met placeholderkosten kloppen jullie marges niet** — vul dus meteen de echte waarden in.

Invullen kan op twee manieren:

```bash
compass costs set        # vragenlijst in de terminal, huidige waarden als default
compass costs show       # huidige model + versiegeschiedenis + break-even ROAS
```

of via het dashboard: **Instellingen → Kostenmodel**. Bedragen typ je gewoon op z'n Nederlands (`6,50`), percentages als procenten (`12,4` voor 12,4%).

### Elk veld uitgelegd

- **Inkoopprijs per eenheid (COGS)** — wat één zakje jou kost tot het in je magazijn ligt: productiekosten **plus** inbound transport, invoerkosten en dergelijke, gedeeld door het aantal zakjes van de batch. Voorbeeld: € 6,50.
- **Verzendkosten per bestelling** — wat het versturen van één bestelling kost, inclusief doosje/envelop. Voor ons: het brievenbuspakje-tarief, bijvoorbeeld € 4,40.
- **Betaalkosten, standaard (% + vast)** — wat de betaalprovider per transactie afroomt als er geen aparte regel voor de betaalmethode staat: een percentage van het orderbedrag plus een vast bedrag. Voorbeeld: 1,5% + € 0,25.
- **Betaalkosten per betaalmethode** — uitzonderingen per methode. iDEAL is bijvoorbeeld meestal een vast bedrag (± € 0,29) en 0%, creditcard juist een hoger percentage. **De exacte tarieven staan in je contract of dashboard bij Mollie / Shopify Payments** — even opzoeken, niet gokken.
- **bol-commissie (%)** — het percentage dat bol inhoudt op de verkoopprijs (incl. btw). Waar te vinden: in het partnerplatform bij tarieven/commissies voor jouw productcategorie (menu kan iets anders heten). Voorbeeld: 12,4%. Op bol-orders rekent Compass géén betaalkosten — bol regelt de betaling, de commissie ís de kostenpost.
- **Btw-tarief** — voor voedingssupplementen in Nederland 9% (het lage tarief); vul in als `9` procent. Compass rekent hiermee de omzet excl. btw uit voor bol-orders en CSV-import (Shopify geeft de btw-splitsing zelf mee). **Laat het btw-tarief bevestigen door jullie boekhouder** — bij twijfel of het 9% of 21% is, is dat een boekhoudersvraag, geen Compass-instelling om mee te experimenteren.
- **Vaste lasten per maand** — alles wat maandelijks doorloopt óngeacht de verkoop: Shopify-abonnement, e-mailsoftware, boekhouder, verzekering, opslag/magazijnhuur. Voorbeeld: € 400. **Niet** meetellen: ad spend en inkoop — die zitten al elders in de berekening, anders tel je ze dubbel.
- **Levertijd (dagen)** — hoeveel dagen er zitten tussen "bestelling plaatsen bij de leverancier" en "voorraad ligt bij ons". Wees eerlijk-pessimistisch: productie + transport + inklaring. Voorbeeld: 30.
- **Veiligheidsfactor** — buffer op het bestelpunt voor als de verkoop tegen de verwachting in versnelt of de levering uitloopt. `1,3` betekent: bestel als de voorraad nog maar 1,3 × de levertijd dekt.
- **Geldig vanaf** — de datum waarop deze cijfers ingaan. Het kostenmodel heeft **versiegeschiedenis**: wordt de inkoop volgend kwartaal goedkoper, dan voer je een nieuwe versie in met de nieuwe datum. Oude orders blijven berekend met de versie die tóén gold — de maandcijfers van maart veranderen dus niet doordat je in juni iets aanpast.
- **Notitie** — vrij veld voor jezelf, bijvoorbeeld "nieuwe leverancier, batch 2027-Q1".

Na het opslaan herrekent Compass alle dagcijfers en toont het de nieuwe **break-even ROAS**. Onthoud dat getal: elke euro ad spend moet minstens zoveel omzet (excl. btw) opleveren.

---

## 7. Dagelijkse run instellen

Compass leeft van dagelijkse runs (daar komen de dagcijfers, de voorraadhistorie en de signalen vandaan). Kies één van de twee routes — dezelfde afweging als bij AdScout. Eén gemiste dag is geen ramp: `compass collect` haalt standaard de laatste 3 dagen opnieuw op (zo komen ook late refunds binnen), en dubbel draaien kan geen kwaad — geen duplicaten.

### Route (a): op je eigen computer (cron of launchd)

Het kant-en-klare script `ops/compass-cron.sh` doet alles: projectmap, venv, `compass collect`, op maandag het weekrapport, en logt naar `logs/cron-compass.log`.

**Cron (macOS en Linux).** Open je crontab met `crontab -e` en voeg toe (vervang het pad door jouw echte projectpad):

```
30 7 * * * /bin/bash /Users/jouwnaam/ads-scraper/ops/compass-cron.sh
```

Dat is: elke dag om **07:30** — een half uur na AdScout's run van 07:00, zodat ze elkaar niet in de weg zitten. Controleer de volgende ochtend `logs/cron-compass.log`.

**Launchd (de nettere macOS-variant).** Sliep je Mac om 07:30, dan draait launchd de run alsnog zodra hij wakker wordt:

```bash
cp ops/nl.cloudplunge.compass.plist ~/Library/LaunchAgents/
open -e ~/Library/LaunchAgents/nl.cloudplunge.compass.plist
```

Vervang in dat bestand de **drie** plekken met `/REPLACE/PAD/NAAR/ads-scraper` door jouw echte projectpad, sla op, en laad de taak:

```bash
launchctl load ~/Library/LaunchAgents/nl.cloudplunge.compass.plist
```

Direct testen: `launchctl start nl.cloudplunge.compass` en dan `logs/cron-compass.log` bekijken. Uitzetten: `launchctl unload ~/Library/LaunchAgents/nl.cloudplunge.compass.plist`.

### Route (b): GitHub Actions (cloud, computer mag uit)

De workflow staat klaar in [`.github/workflows/compass-collect.yml`](../.github/workflows/compass-collect.yml) en draait dagelijks om 05:45 UTC (07:45 NL-zomertijd) plus handmatig via **Actions → Compass daily collect → Run workflow**.

> **WAARSCHUWING: de repository MOET privé zijn.** De workflow commit de database met jullie omzet- en margecijfers terug naar de repo, en de tokens staan in de repo-secrets. Check op GitHub onder Settings dat er "Private" bij de repo staat vóór je deze route gebruikt.

Setup:

1. Push dit project naar een **privé** GitHub-repository (waarschijnlijk al gebeurd voor AdScout).
2. Zet de geheimen als **secrets**: op GitHub **Settings → Secrets and variables → Actions → New repository secret**, voor elk van:
   - `SHOPIFY_ACCESS_TOKEN` (sectie 3)
   - `META_ACCESS_TOKEN` (sectie 4 — staat er mogelijk al voor AdScout)
   - `BOL_CLIENT_ID` en `BOL_CLIENT_SECRET` (sectie 5)
3. Zet de niet-geheime instellingen als **variables**: zelfde plek, tabblad **Variables**:
   - `SHOPIFY_SHOP` (bijv. `jouwwinkel.myshopify.com`)
   - `META_AD_ACCOUNT_ID` (bijv. `act_1234567890`)
4. **Eenmalige bootstrap van de database.** De cloud-runner heeft een database met jullie kostenmodel nodig. Draai lokaal `compass init` en `compass costs set` ([sectie 6](#6-kostenmodel-invullen)) en commit de database één keer expliciet mee (hij is normaal gitignored, vandaar `-f`):

   ```bash
   git add -f data/compass.db
   git commit -m "Bootstrap: Compass-database met kostenmodel"
   git push
   ```

   Zonder deze stap weigert de workflow bewust te draaien (foutmelding in de Actions-log) — zonder kostenmodel zou hij dagelijks "groen" zijn terwijl er niets bruikbaars berekend wordt.
5. Klaar — test direct via **Actions → Compass daily collect → Run workflow**.

Hoe het werkt: elke run installeert Compass op een tijdelijke machine, draait `compass collect` (en op maandag `compass report --weekly`) en **commit daarna `data/` en `reports/` terug naar de repo** met een commit als `[data] compass daily collect 2026-07-01`. Wil je de data lokaal bekijken, doe dan eerst `git pull` en start `compass web`. Verloopt het Meta-token, dan zie je dat in de Actions-log; vernieuw dan óók de secret.

---

## 8. CSV-import

Voor alles wat niet via een API binnenkomt: omzet van vóór de koppelingen, een verkoopkanaal zonder API (beurs, eigen fysieke verkoop), of handmatige voorraadtellingen.

```bash
compass import bestand.csv                 # herkent het type aan de kolomkoppen
compass import bestand.csv --type orders   # of expliciet: orders | spend | inventory
```

Praktisch: komma óf puntkomma als scheidingsteken werkt, bedragen mogen op z'n Nederlands (`29,95`) of met een punt (`29.95`), en een CSV die je uit **Excel** opslaat (UTF-8, ook "met BOM") werkt gewoon. Importeren is idempotent: hetzelfde bestand twee keer importeren geeft geen dubbele data. Foute regels worden per regel gemeld; de rest gaat gewoon door.

### Orders

Verplichte kolommen: `extern_id`, `channel` (`shopify` of `bol`), `ordered_at`, `gross_eur` (incl. btw). Optioneel: `units`, `payment_method`, `status` (`paid`/`refunded`), `refunded_eur`, `customer_ref`.

```
extern_id;channel;ordered_at;gross_eur;units;payment_method;status;refunded_eur;customer_ref
1001;shopify;2026-06-15T14:32:00+02:00;29,95;1;ideal;paid;0;klant@voorbeeld.nl
1002;bol;2026-06-15;59,90;2;;paid;;
1003;shopify;2026-06-16;29,95;1;ideal;refunded;29,95;klant@voorbeeld.nl
```

Alleen een datum (zonder tijd) mag ook. `customer_ref` (e-mail of klantnummer) wordt **direct omgezet naar een hash** en nooit als tekst opgeslagen — het dient alleen om terugkerende klanten te herkennen.

### Ad spend

Verplicht: `day`, `campaign_id`, `campaign_name`, `spend_eur`. Optioneel: `impressions`, `clicks`, `meta_purchases`, `meta_purchase_value_eur`.

```
day;campaign_id;campaign_name;spend_eur;impressions;clicks;meta_purchases;meta_purchase_value_eur
2026-06-15;120210000000001;Prospecting;250,00;41000;620;8;239,60
```

### Voorraad

Verplicht: `day`, `units`. Optioneel: `source` (standaard `manual`).

```
day;units
2026-06-15;1830
```

---

## 9. Weekrapport en export

**Weekrapport** — de kerncijfers van de afgelopen 7 dagen naast de week ervoor, "volgens Meta vs werkelijk", actieve signalen, de voorraadstand en drie zinnen in gewone taal over wat het betekent:

```bash
compass report
```

Dit schrijft twee bestanden in `reports/`: `compass-week-<datum>.md` (Markdown) en `compass-week-<datum>.html` (open je in de browser, prima te printen of door te sturen). De geplande run uit [sectie 7](#7-dagelijkse-run-instellen) maakt dit rapport automatisch elke maandag.

**Export** — alle data zonder lock-in, voor eigen analyses in Excel/Numbers/Google Sheets:

```bash
compass export --csv                       # export/compass-<tabel>.csv
compass export --json                      # zelfde data als JSON
compass export --csv --out ~/Desktop/cp    # andere doelmap
```

Er komt één bestand per tabel uit: orders, ad spend per campagne per dag, de dagcijfers, het kostenmodel (alle versies) en de signalen.

---

## 10. Backup en herstel

Alles wat waardevol is, staat op drie plekken:

| Wat | Waarom |
|---|---|
| `data/compass.db` | de volledige database: orders, ad spend, voorraad, kostenmodel, signalen |
| `.env` | je tokens en instellingen (gedeeld met AdScout) |
| `reports/` | de weekrapporten (zijn opnieuw te genereren, maar handig om te bewaren) |

**Backup maken:** sluit eerst het dashboard en wacht tot een lopende `compass collect` klaar is, en kopieer dan simpelweg de hele `data/`-map plus het `.env`-bestand naar een andere plek. De `data/`-map bevat ook de AdScout-database — één kopie dekt dus beide tools:

```bash
cp -r data ~/Backups/cloudplunge-backup-$(date +%F)
cp .env ~/Backups/cloudplunge-backup-$(date +%F)/
```

**Advies: zet wekelijks een kopie op een cloud-drive** (Google Drive, Dropbox, iCloud), net als bij AdScout. Dit is jullie financiële historie — die wil je niet kwijt bij een kapotte laptop.

**Herstellen:** installeer volgens de Snelstart (sectie 2, stap 1–2), zet de gebackupte `data/`-map en `.env` terug in de projectmap, en draai `compass status` om te controleren dat alles er weer is. Er is geen aparte import-stap.

Gebruik je de GitHub Actions-route uit sectie 7, dan ís de (privé) repo je backup; alleen `.env` bewaar je dan nog apart.

---

## 11. Signalen uitgelegd

Signalen zijn Compass' manier om te zeggen: "kijk hier even naar". Ze verschijnen op het dashboard (**Signalen**, met een badge in het menu) en in het weekrapport, altijd mét de cijfers erachter. Een signaal doet zelf niets — het advies uitvoeren is en blijft een menselijke beslissing.

> Dit zijn vuistregels op basis van jullie eigen cijfers, geen financieel advies — jullie beslissen.

**Opschalen** — *"Overweeg het advertentiebudget stapsgewijs te verhogen (bijv. +20%)."*
Vuurt als het 7 dagen op rij structureel goed gaat: de MER over de voorbije 7 dagen is minstens **1,2 × de break-even ROAS** (je verdient dus ruim aan je advertenties), én de kosten per nieuwe klant (CAC) zijn niet aan het oplopen ten opzichte van een week eerder. Extra slimmigheid: is je **voorraad te laag om groei aan te kunnen**, dan wordt dit signaal onderdrukt en krijg je in plaats daarvan het voorraadsignaal — met de uitleg dat de advertentiecijfers goed genoeg zijn om op te schalen, maar dat je eerst moet bijbestellen. Anders adverteer je straks voor een lege plank.

**Afschalen** — *"Je verliest geld op advertenties; overweeg het budget te verlagen of de slechtste campagne te pauzeren."*
Vuurt als de MER 7 dagen op rij **onder de break-even ROAS** ligt: elke advertentie-euro brengt dan minder marge terug dan hij kost. De uitleg noemt de zwakste campagne van de afgelopen week (op basis van Meta's eigen attributie, dus "volgens Meta" — check het altijd zelf in Ads Manager).

**Voorraad bestellen** — *"Bestel nieuwe voorraad; bij de huidige verkoopsnelheid ben je over X dagen uitverkocht (rond DATUM)."*
Compass berekent de verkoopsnelheid over de laatste 30 dagen (recente dagen tellen zwaarder) en vuurt zodra de resterende voorraad minder dagen dekt dan **levertijd × veiligheidsfactor** uit het kostenmodel. Met 30 dagen levertijd en factor 1,3 is het bestelpunt dus 39 dagen voorraad.

**Spend-afwijking** — *"Let op: de spend van gisteren (€X) is meer dan 2× het 7-daagse gemiddelde (€Y)."*
Een vangnet tegen ongelukken: een campagne die op hol slaat, een per ongeluk verhoogd budget, een verkeerd ingestelde einddatum. Vuurt als de spend van gisteren meer dan 2× het gemiddelde van de week ervoor was.

### Drempels

Alle drempels zijn aan te passen via het dashboard (**Instellingen → Signaal-drempels**) — de standaardwaarden zijn bewust voorzichtig:

| Drempel | Standaard | Betekenis |
|---|---|---|
| MER-factor voor opschalen | 1,2 | opschalen pas bij MER ≥ 1,2 × break-even ROAS |
| CAC-tolerantie | 1,05 | CAC mag max 5% hoger zijn dan een week eerder |
| Spend-afwijkingsfactor | 2,0 | alarm bij dagspend > 2× het 7-daagse gemiddelde |
| Reeks (dagen) | 7 | zoveel dagen op rij moet de MER-conditie gelden |
| Minimum spend | € 25/dag | daaronder zeggen de MER-regels niets en blijven ze stil |
| Cooldown (dagen) | 7 | hetzelfde signaaltype vuurt max 1× per 7 dagen |

Onder de € 25 spend per dag is de data te dun om conclusies uit te trekken — dan blijft Compass bewust stil in plaats van te gokken.

---

## 12. Troubleshooting

**"Shopify: 401/403" of "credentials-probleem" bij Shopify**
Het token is verkeerd gekopieerd, ingetrokken, of de app mist een scope. Oplossing: `compass verify shopify` draaien en de melding lezen; zo nodig in de Shopify-admin het token opnieuw genereren of de scopes uit [sectie 3](#3-shopify-custom-app-aanmaken) aanvinken en de app opnieuw installeren.

**"Token verlopen of ongeldig (Meta error 190)"**
Het gedeelde Meta-token verloopt na ±60 dagen. Vernieuwen zoals altijd: root-README, [sectie "Meta developer-app en token"](../README.md#3-meta-developer-app-en-token), kopje "Token vernieuwen" — dat lost het in één keer op voor AdScout én Compass. Gebruik je GitHub Actions, vernieuw dan ook de secret `META_ACCESS_TOKEN`.

**bol geeft "429" / te veel verzoeken**
De bol-API begrenst het aantal calls. Compass wacht vanzelf en probeert het opnieuw (backoff); faalt de run alsnog, dan is de oplossing simpel: **later gewoon opnieuw `compass collect` draaien**. Runs zijn idempotent — dubbel draaien geeft geen dubbele data.

**"Geen kostenmodel"**
`compass collect` weigert bewust te draaien zonder kostenmodel — zonder kosten zijn alle marges onzin. Oplossing: `compass init` draaien (zet een placeholder neer) en daarna het echte model invullen via [sectie 6](#6-kostenmodel-invullen).

**Lege dashboards**
Meestal: er is nog geen data verzameld. Check `compass status` — staan de bronnen op "geconfigureerd", en is er een geslaagde run? Draai `compass backfill --days 365` voor de historie en herlaad de pagina. Let ook op de demo-vlag: `compass web --demo` kijkt naar de demo-database, `compass web` naar de echte — als de één vol staat en de ander leeg lijkt, kijk je waarschijnlijk naar de verkeerde.

**"Volgens Meta" wijkt af van de werkelijke omzet**
Dat is **normaal en geen bug**. Meta telt aankopen toe aan advertenties volgens zijn eigen attributieregels (wie een ad zag of aanklikte en later kocht) en mist tegelijk kopers die niet te volgen zijn (cookieweigering, iOS-privacy). Daarom toont Compass beide getallen altijd naast elkaar en rekent het voor beslissingen met de **werkelijke** omzet uit Shopify + bol. Groeit het gat ineens sterk, dan is dát interessant: mogelijk is de pixel stuk of de attributie-instelling veranderd.

**"database is locked"**
SQLite laat maar één schrijver tegelijk toe. Dit gebeurt vrijwel alleen als het dashboard openstaat terwijl `compass collect` draait. Oplossing: dashboard sluiten (Ctrl+C) en het commando **gewoon opnieuw draaien**. Er gaat niets verloren.

**`pip list` toont zowel "adscout" als "cloudplunge-tools"**
Op computers waar AdScout al stond, kan de oude registratie "adscout 0.1.0" blijven staan naast de nieuwe "cloudplunge-tools" (die beide tools bevat). Dat is onschuldig — maar draai **nooit** `pip uninstall adscout`: dat verwijdert dan ook het `adscout`-commando dat de nieuwe installatie gebruikt. Gebeurt dat toch per ongeluk, dan herstelt `pip install -e .` alles weer.

**Waar staan de logs?**
- `logs/compass.log` — alles wat Compass doet, met detail (roteert vanzelf).
- `logs/cron-compass.log` — de output van de geplande dagelijkse runs (route a uit sectie 7).
- `compass status` — de laatste runs uit de database: wanneer, hoeveel orders/spend-regels, welke fouten.

---

## 13. Onderhoud

### Dependencies veilig updaten

Compass en AdScout delen dezelfde vastgepinde pakketten (`requirements.txt` + `pyproject.toml`), dus het update-ritueel is één keer werk voor beide tools. Volg de stappen in de [root-README, sectie "Onderhoud"](../README.md#10-onderhoud): pins verhogen → installeren → `pytest` → alleen bij groen committen.

### API-versies ophogen

- **Shopify** ondersteunt elke Admin API-versie ±12 maanden. Symptoom van een verlopen versie: Shopify-foutmeldingen die over de versie klagen. De versie staat op één plek: `SHOPIFY_API_VERSION` in `.env` (nu `2025-04`). Ophogen = daar een nieuwere versie invullen en `compass verify shopify` draaien.
- **Meta**: `META_GRAPH_VERSION` in `.env`, gedeeld met AdScout — zie de root-README, sectie "Graph API-versie ophogen".
- **bol** versioneert via de code (Retailer API v10, in `compass/sources/bol.py`); bol kondigt nieuwe versies ruim van tevoren aan (zie [developers.bol.com](https://developers.bol.com/)). Dit is de enige versie-bump waar een regel code voor moet worden aangepast.

### Databasemigraties

Zelfde regels als AdScout. Schemawijzigingen staan als genummerde SQL-bestanden in `compass/migrations/` en worden automatisch toegepast zodra Compass start:

- **Nooit een oud (al toegepast) migratiebestand wijzigen.**
- Iets veranderen aan het schema = een **nieuw** bestand met het volgende nummer toevoegen.

### Bijpraten na stilstand

Laptop drie weken dicht geweest? Eén commando haalt alles in:

```bash
compass backfill --days 30
```

Backfill is net zo veilig als collect: bestaande data wordt bijgewerkt, niets dubbel.

---

## 14. Compliance en privacy

- **Compass is alleen-lezen, by design.** De Shopify-app heeft uitsluitend read-scopes (kán dus niets aanpassen), de Meta-permissie `ads_read` is alleen-lezen, en van de bol Retailer API gebruikt Compass uitsluitend lees-endpoints. Compass wijzigt nooit campagnes, budgetten, prijzen of orders.
- **Minimale toegang.** Per bron vragen we alleen de scopes die nodig zijn (zie secties 3–5); niets meer. Tokens staan alleen in `.env` (gitignored) en — bij de Actions-route — in de secrets van de privé-repo. Niet delen buiten het bedrijf.
- **AVG / klantgegevens.** Compass rekent en toont nergens namen of e-mailadressen: klantidentiteit bestaat in alle berekeningen en schermen alleen als een onomkeerbare hash ("is dit dezelfde klant als eerder?"). Wél bewaart de database de ruwe orderberichten zoals de bron ze aanleverde (nooit brondata weggooien) — daarin kán klantinfo zitten. Behandel `data/` daarom als wat het is: dezelfde vertrouwelijke administratie als je Shopify-account zelf, lokaal of in de privé-repo, nooit ergens publiek.
- **Eigen data, geen scraping.** Alles wat Compass ophaalt is onze eigen bedrijfsdata, via de officiële API's van de platformen waar we zelf klant zijn, binnen hun voorwaarden en rate limits (Compass remt zichzelf af).
- **Geen financieel advies, geen boekhouding.** Alle marges en netto-indicaties zijn vuistregels op basis van het zelf ingevulde kostenmodel. Voor belastingen, jaarcijfers en officiële winstbepaling is de boekhouder leidend.
