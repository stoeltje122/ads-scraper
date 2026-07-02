# Goedemorgen! ☀️ — Pulse staat klaar

Vannacht is **Pulse** gebouwd: jullie feedback- en luistertool. Alles wat zonder
wachtwoorden en sleutels kon, is af én getest (234 automatische tests, allemaal
groen). Dit bestand vertelt in gewone taal wat er staat, hoe je het nu meteen
kunt zien werken, en welke stappen alleen jullie kunnen doen.

## Eerst even kijken (2 minuten, niets nodig)

Open een terminal in de map `ads-scraper` en typ:

```bash
source .venv/bin/activate     # als je dat voor AdScout ook al deed
pulse demo
pulse web
```

Open dan <http://localhost:8010> in de browser. Je ziet het dashboard gevuld met
realistische **voorbeelddata** (verzonnen mails, reacties en reviews):

- **Urgent** — het startscherm. Bovenaan, rood gemarkeerd: meldingen die op een
  bijwerking of gezondheidsvraag kunnen wijzen (bijv. "Hartkloppingen na
  inname"). Dit is de belangrijkste functie van de hele tool. Afgehandeld?
  Klik "Markeer als opgevolgd".
- **Inbox** — alle feedback, met filters en zoekveld.
- **Trends** — grafieken: volume, sentiment, top-klachten vs vorige week.
- **Kansen** — waar klanten van concurrenten over klagen (bijv. 8hours:
  "geen effect") en welke advertentie-invalshoek daaruit volgt.
- **Import** — hier plakken jullie straks échte reviews in.
- **Beheer** — bronnen, concurrenten, thema's, retentie, "iemand vergeten" (AVG).

Stoppen: `Ctrl+C` in de terminal. De voorbeelddata verdwijnt zodra jullie met
echte data beginnen: verwijder `data/pulse.db*` en draai `pulse init`.

> Naast dit bestand is er een volledige handleiding: **[PULSE.md](PULSE.md)** —
> geschreven zoals de AdScout-README, stap voor stap, voor niet-techneuten.

## Afvinklijst: wat alleen jullie kunnen doen

Per stap staat erbij hoe lang het duurt en waar de uitleg staat. Volgorde maakt
niet uit; alles wat nog niet gedaan is, blijft gewoon netjes wachten (de rest
werkt intussen door).

- [ ] **1. Gmail koppelen** (±15 min, eenmalig) — Google Cloud-project +
  OAuth-scherm + credentials-bestand, daarna één keer inloggen in de browser.
  Complete walkthrough met schermnamen: **PULSE.md sectie 4**. Daarna:
  `pulse verify gmail` → `pulse source activate gmail`.
- [ ] **2. Anthropic-sleutel voor de AI-analyse** (±5 min) — aanmaken op
  <https://console.anthropic.com> en in `.env` zetten. **Let op: analyse kost
  een klein bedrag per gebruik** (standaardmodel is bewust een goedkoop
  Claude-model; reken op hooguit enkele euro's per maand — stel in de console
  een maandlimiet in, bijv. $10). Uitleg: **PULSE.md sectie 6**. Zonder sleutel
  blijft alles werken; items wachten in de analyse-wachtrij en het
  trefwoord-vangnet voor gezondheidssignalen werkt sowieso.
- [ ] **3. Meta Page-token** (±10 min) — voor reacties op eigen Facebook/
  Instagram-posts en -ads. Hergebruikt de AdScout-app. Walkthrough:
  **PULSE.md sectie 5**. Daarna: `pulse verify meta_comments` →
  `pulse source activate meta_comments`.
- [ ] **4. Trustpilot & bol: niets in te stellen** — dit is vannacht écht
  uitgezocht (bronnen in PULSE.md sectie 7): Trustpilot heeft geen gratis API
  (alleen betaalde plannen, duizenden euro's per jaar) en de bol Retailer API
  geeft geen reviewteksten. Scrapen is bij beide verboden — dus dat doet Pulse
  bewust niet. **De route is handmatige import**: één keer per maand reviews
  kopiëren en plakken via het dashboard (5 min). De twee bronnen staan daarom
  als "gepauzeerd" in het systeem, met de reden erbij.
- [ ] **5. Review-URL's van concurrenten invullen** (±10 min, handig als
  geheugensteun) — dashboard → Beheer → Concurrenten → bewerk, of
  `pulse competitor set-urls "Cloudpillo" <url>`. De negen concurrenten staan
  er al in (gedeeld met AdScout).
- [ ] **6. Dagelijkse run aanzetten** (±5 min) — kies: Mac (launchd), server
  (cron) of GitHub Actions. Kant-en-klare bestanden staan in `ops/`;
  instructies: **PULSE.md sectie 9**. Draait AdScout al om 07:00? Pulse staat
  standaard op 07:30 zodat ze elkaar niet in de weg zitten.
- [ ] **7. Eerste echte reviews importeren** (wanneer jullie willen) — kopieer
  jullie eigen Trustpilot/bol-reviews en wat concurrent-reviews en plak ze in
  dashboard → Import. Draai daarna `pulse analyze` (of wacht op de nachtelijke
  run). Vanaf dat moment vult Kansen zich met échte inzichten.

## Wat er vannacht precies gebouwd is

- Compleet datamodel met migraties (bronnen, concurrenten, items, analyses,
  mail-conversaties, weekrapporten, runs-audittrail) in een eigen database
  `data/pulse.db` — AdScout blijft onaangeraakt.
- Drie volwaardige adapters (Gmail readonly, Meta-reacties, handmatige import)
  plus voorbereide plekken voor Trustpilot/bol, elk met een `verify()` die in
  gewone taal zegt wat werkt. Alles ook offline getest op voorbeelddata.
- AI-analysepipeline (Claude API): sentiment, thema's, urgentie, type,
  gezondheidsflag, en per concurrent-review het genoemde pluspunt/de klacht.
  Mislukte analyses worden nooit stilletjes overgeslagen.
- Gezondheids-vangnet met trefwoorden dat al vóór de AI-analyse werkt.
- Dashboard met 7 schermen, weekrapport (Markdown + HTML), export (CSV/JSON),
  `pulse forget` (AVG), retentie-opschoning, logging, en een `pulse status`
  die het hele systeem in één blik samenvat.
- 234 automatische tests; `pulse demo` als altijd-werkende rooktest.

## Aannames die vannacht gemaakt zijn (met defaults uit de opdracht)

1. **Mail-backfill 90 dagen**, mailbox `info@cloudplunge.com`, alleen inkomend,
   nieuwsbrieven/notificaties en onze eigen antwoorden automatisch weggefilterd.
2. **Retentie 24 maanden** (aanpasbaar in Beheer), opschoning in de dagelijkse run.
3. **Analysemodel**: kostenefficiënt Claude-model (Haiku) als standaard,
   instelbaar via `PULSE_ANTHROPIC_MODEL` in `.env`. De AdScout-instelling
   (`ANTHROPIC_MODEL`) blijft apart; de API-sleutel delen ze wél.
4. **Statussen intern in het Engels** (`active`/`awaiting_config`/`paused`),
   overal in beeld vertaald naar "actief / wacht op configuratie / gepauzeerd" —
   zelfde conventie als AdScout (Engelse code, Nederlandse interface).
5. **Concurrent-items komen niet in het Urgent-scherm**, ook niet bij een
   gezondheidsklacht over een ander merk: dat is een inzicht voor Kansen, geen
   supportcase van ons. (De AI markeert hem wel, dus hij is terugvindbaar via
   de Inbox-filter "gezondheid".)
6. **Het trefwoord-vangnet is heilig**: een melding met een gezondheids-
   trefwoord blijft in Urgent staan tot een mens hem afvinkt — óók als de
   AI oordeelt dat het geen gezondheidssignaal is (bijv. "mijn hoofdpijn is
   juist wég!"). Trefwoord + AI-twijfel = twijfel, en bij twijfel kijken
   jullie even zelf. Op de detailpagina staat wélk trefwoord de markering
   veroorzaakte; afvinken kost één klik.
7. **Handmatig geïmporteerde Trustpilot/bol-reviews** worden onder die kanalen
   geregistreerd (filters kloppen dus), ook al zijn die bronnen "gepauzeerd" —
   pauze gaat alleen over automatisch ophalen.
8. **Pulse draait op poort 8010** zodat het naast het AdScout-dashboard (8000)
   kan; de dagelijkse run staat op 07:30 (AdScout: 07:00).
9. **bol sterren-tracking per EAN** (officieel toegestaan, ook van
   concurrentproducten) is bewust NIET gebouwd vannacht — het is een mooie
   uitbreiding zodra jullie er waarde in zien; de plek in de code is er klaar
   voor (`pulse/sources/bol.py`).
10. **GitHub Actions-workflow staat uit** tot jullie hem bewust aanzetten: die
    route commit de database (met klantdata) naar de repo en vereist dus een
    privérepo + eenmalige bootstrap; het Gmail-token gaat als geheim (secret),
    nooit in git. Cron/launchd lokaal is simpeler en priver; de keuze is aan
    jullie.

## Eerlijk overzicht: wat is er níet (af)

- Geen automatische Trustpilot/bol-koppeling — niet vergeten, maar bewust
  (zie punt 4 hierboven; het kán niet legitiem zonder duur betaald plan).
- Rapporten worden nog niet gemaild/geappt: ze staan op schijf en in het
  dashboard. De aansluiting daarvoor (`Notifier`) is voorbereid.
- De AI-analyse is nog niet met een échte sleutel getest (die is er nog niet) —
  wel volledig met een nagemaakte analyse-motor. Eerste echte run: gewoon
  `pulse analyze` na stap 2, en kijk even mee of de labels kloppen.

Fijne dag! — en begin bij `pulse demo` ☕
