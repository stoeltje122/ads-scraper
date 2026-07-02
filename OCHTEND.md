# Goedemorgen! ☀️ — Compass staat klaar

Vannacht is **Compass** gebouwd: het financiële stuur van Cloudplunge. Het
staat naast AdScout in deze map en is af — alles draait al op demo-data.
Jullie hoeven vanochtend alleen nog "sleutels erin" te doen (zie de
afvinklijst hieronder) en het kostenmodel in te vullen.

---

## 1. Eerst even kijken (2 minuten)

Open een terminal in deze map en typ:

```bash
compass demo
compass web --demo
```

Open daarna in de browser: **http://127.0.0.1:8010**

Je ziet dan het volledige dashboard, gevuld met 90 dagen realistische
voorbeeldcijfers (verzonnen, maar herkenbaar: €29,95-orders via Shopify en
bol, Meta-campagnes, voorraad die opraakt). Er staat een groene balk boven
elke pagina: *"Demo-modus: dit zijn gegenereerde voorbeeldcijfers"* — de
demo leeft in een eigen databasebestand en kan nooit tussen echte cijfers
terechtkomen.

Let bij het rondklikken op:

- **Vandaag** — omzet per kanaal, MER vs break-even ROAS (het groene
  "Gezond"-blok), en het paneel *"Volgens Meta vs werkelijk"*: Meta claimt
  altijd minder (of soms meer) dan er echt verkocht is; Compass toont
  beide eerlijk naast elkaar en doet nooit alsof Meta's cijfer de omzet is.
- **Signalen** — er staat een echt signaal klaar: *"Bestel nieuwe
  voorraad"*, inclusief de kruisregel ("advertenties zijn goed genoeg om
  op te schalen, maar bestel eerst voorraad bij").
- **Economie** — de opbouw van één gemiddelde order: van omzet naar
  contributiemarge, apart voor Shopify en bol.
- **Instellingen** — hier vullen jullie straks het echte kostenmodel in.

Stoppen: `Ctrl+C` in de terminal.

---

## 2. Afvinklijst per bron (het "mens-werk")

Voor elke bron staat een stap-voor-stap in **compass/README.md**. De korte
versie:

### ☐ Shopify (± 5 minuten)
1. Shopify-admin → Instellingen → Apps en verkoopkanalen → Apps
   ontwikkelen → app "Compass" aanmaken.
2. Alleen lees-scopes aanvinken: `read_orders`, `read_products`,
   `read_inventory`.
3. Token (`shpat_…`) kopiëren → in `.env` zetten (`SHOPIFY_SHOP` +
   `SHOPIFY_ACCESS_TOKEN`).
4. Test: `compass verify shopify`
   *(let op: voor historie ouder dan ±60 dagen heeft de app ook de scope
   `read_all_orders` nodig — zie README, sectie Shopify)*

### ☐ Meta (± 5 minuten — hergebruikt de AdScout-app)
1. Zelfde Meta-app als AdScout; het token heeft wél de extra permissie
   **ads_read** nodig (zie README, sectie "Meta Marketing API koppelen").
2. Advertentieaccount-ID opzoeken in Ads Manager (begint met `act_…`)
   → in `.env` als `META_AD_ACCOUNT_ID`.
3. Test: `compass verify meta`

### ☐ bol (± 10 minuten)
1. partner.bol.com → Instellingen → API-instellingen → client credentials
   aanmaken.
2. In `.env`: `BOL_CLIENT_ID` + `BOL_CLIENT_SECRET`.
3. Test: `compass verify bol`

### ☐ Kostenmodel invullen (het belangrijkste kwartier van vandaag)
`compass init` zet een **placeholder**-kostenmodel klaar; de marges op het
dashboard zijn pas echt als jullie de echte cijfers invullen via
`compass costs set` of via het dashboard (Instellingen). Zoek deze
bedragen op:

| Veld | Wat is het | Placeholder van vannacht |
|---|---|---|
| Inkoopprijs per zakje | wat één zakje (60 caps) jullie kost, inclusief inslag | **€ 6,50 — INVULLEN** |
| Verzendkosten per order | brievenbuspakje incl. verpakking | **€ 4,40 — INVULLEN** |
| Betaalfee per methode | staat in jullie Mollie/Shopify Payments-contract | **iDEAL € 0,29; standaard 1,5% + € 0,25 — INVULLEN** |
| bol-commissie | staat in het bol-partnerdashboard bij jullie categorie | **12,4% — INVULLEN** |
| Vaste lasten per maand | Shopify-abonnement, tools, boekhouder, … | **€ 400 — INVULLEN** |
| Levertijd leverancier | besteldag → voorraad binnen | **30 dagen** |
| Btw-tarief | zie hieronder | **9% — BEVESTIGEN** |

Na het invullen herrekent Compass automatisch alle marges, óók met
terugwerkende kracht (elke order gebruikt het kostenmodel dat op zijn
besteldatum gold — wijzig je later iets, maak dan een nieuwe versie aan
met de juiste ingangsdatum).

### ☐ Eerste echte run
```bash
compass backfill --days 365   # historie ophalen (mag ook eerst 90)
compass web                   # het echte dashboard (zonder --demo)
```
En daarna dagelijks automatisch: zie compass/README.md, sectie
"Dagelijkse run instellen" (cron/launchd of GitHub Actions — zelfde keuze
als bij AdScout).

---

## 3. Aannames van vannacht (even checken)

1. **Btw 9%.** Voedingssupplementen vallen in NL doorgaans onder het
   verlaagde tarief, maar **laat dit bevestigen door de boekhouder** —
   het staat ook als noot in het dashboard en de README. Aanpassen kan in
   Instellingen (één veld).
2. **MER wordt gerekend op omzet exclusief btw.** Bewust: de break-even
   ROAS (1 ÷ marge-ratio) is ook op omzet excl. btw gebaseerd, dus alleen
   zo is de vergelijking zuiver. Meta's eigen ROAS (op basis van
   pixelwaarde incl. btw) staat er altijd apart naast, gelabeld "volgens
   Meta", en wordt nooit met de break-even vergeleken.
3. **Volledig terugbetaalde orders tellen als 0** — geen omzet én geen
   kosten. In werkelijkheid zijn verzendkosten van een retour "weg"; dat
   verfijnen kan later, nu is het simpel en eerlijk.
4. **bol-klanten tellen als nieuwe klant** (bol maskeert de identiteit;
   herhaling is niet herkenbaar). Shopify-klanten worden herkend via een
   geanonimiseerde hash — er worden nergens namen, adressen of
   e-mailadressen opgeslagen (AVG).
5. **Meta-attributie**: Compass gebruikt de standaardinstelling van
   jullie advertentieaccount en rapporteert wat Meta zelf claimt. Het
   verschil met de werkelijkheid is normaal en wordt overal getoond,
   nooit verstopt.
6. **Placeholder-kostenmodel** (tabel hierboven): tot jullie de echte
   waarden invullen zijn marges, break-even ROAS en signalen indicatief.
7. **Dashboard-stijl**: net als AdScout — geen JavaScript-frameworks of
   externe bronnen (werkt over 5 jaar nog, ook offline). In plaats van
   HTMX gebruikt Compass hetzelfde beproefde formulieren-patroon als
   AdScout; functioneel identiek.
8. **Tijdzone Europe/Amsterdam**: een "dag" is de kalenderdag zoals
   jullie die in Shopify/bol/Meta zien.
9. **Poort 8010** voor het Compass-dashboard, zodat AdScout (poort 8000)
   tegelijk kan draaien.
10. **Eén product**: het datamodel kan later meerdere producten aan
    (nieuwe migratie), maar vandaag rekent alles met het 60-caps-zakje.

---

## 4. Wat er precies gebouwd is

- **`compass` CLI**: `init` · `demo` · `collect` · `backfill` · `costs` ·
  `report --weekly` · `status` · `verify` · `import` (CSV-vangnet) ·
  `export` · `web`. Alles alleen-lezen richting Shopify/Meta/bol; Compass
  wijzigt nooit campagnes, budgetten, prijzen of orders.
- **Dashboard** (7 pagina's): Vandaag, Maand (indicatieve P&L), Advertenties,
  Economie (unit-economics), Voorraad, Signalen, Instellingen.
- **Rekenhart** (`compass/metrics.py`): alle formules op één plek, in
  centen, met 45+ doorgerekende tests (btw-randgevallen, refunds,
  bol-commissie, kostenmodel-versies).
- **Signalen** (advies, nooit actie): opschalen (MER ≥ 1,2× break-even, 7
  dagen, stabiele CAC), afschalen (MER < break-even), voorraad bestellen
  (bestelpunt = levertijd × verkoopsnelheid × 1,3) en spend-afwijking —
  drempels aanpasbaar in Instellingen. Met altijd de zin erbij: *dit zijn
  vuistregels, geen financieel advies — jullie beslissen.*
- **Weekrapport**: `compass report --weekly` → markdown + html in
  `reports/`, met "wat betekent dit" in drie gewone zinnen.
- **Testdekking**: 390 tests, allemaal groen (AdScout's eigen 87 draaien
  onaangetast mee).
- **Backup** = het bestand `data/compass.db` kopiëren (staat in README).

Wat bewust NIET is gebouwd: bankkoppeling (buiten scope), boekhouding
(Compass zegt dat er zelf ook overal bij), schrijf-toegang tot welk
extern systeem dan ook.

## 5. Niet af / eerlijk vermeld

- De drie API-koppelingen zijn volledig gebouwd én getest tegen
  realistische voorbeelddata, maar hebben vannacht uiteraard nog niet
  tegen jullie échte accounts gedraaid. `compass verify` is precies
  daarvoor: het vertelt per bron in gewone taal of de koppeling werkt en
  wat er eventueel mist.
- Weekrapport-notificaties (mail/Slack) zijn voorbereid
  (Notifier-interface) maar nog niet aangesloten — het rapport staat
  gewoon als bestand klaar.
- Shopify-historie ouder dan ±60 dagen vereist de extra scope
  `read_all_orders` (zie README); anders haalt de backfill gewoon op wat
  mag en meldt dat eerlijk.

Veel plezier ermee. — Compass 🧭
