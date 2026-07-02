"""Regenerate the Pulse fixture files (committed sample data).

Run from the repo root after changing anything here:
    .venv/bin/python tests/fixtures/pulse/generate_fixtures.py

The fixtures power `pulse demo`, the dashboard preview and the test suite.
They are shaped exactly like the real API responses so the same parsing
code runs in tests as in production. All people, addresses and texts are
invented. Dates are fixed (late June 2026) so tests stay deterministic.

analyses_canned.json is keyed by external_id and generated *through the
real parsers*, so the keys can never drift from what `pulse demo` stores.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent

PAGE_ID = "104477112233445"
MAILBOX = "info@cloudplunge.com"


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def epoch_ms(iso: str) -> str:
    return str(int(datetime.fromisoformat(iso + "+00:00").timestamp() * 1000))


def gmail_message(
    msg_id: str,
    thread_id: str,
    when: str,                      # 'YYYY-MM-DDTHH:MM:SS' (UTC)
    from_: str,
    subject: str,
    body: str,
    html: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": from_},
        {"name": "To", "value": f"Cloudplunge <{MAILBOX}>"},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": datetime.fromisoformat(when + "+00:00").strftime(
            "%a, %d %b %Y %H:%M:%S +0000")},
    ]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})
    mime = "text/html" if html else "text/plain"
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": ["INBOX"],
        "snippet": " ".join(body.split())[:100],
        "internalDate": epoch_ms(when),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "body": {"size": 0},
            "parts": [
                {"mimeType": mime, "body": {"size": len(body), "data": b64url(body)}}
            ],
        },
    }


GMAIL_MESSAGES = [
    # 1 — THE health signal: possible side effect, must top the Urgent view.
    gmail_message(
        "msg-health-01", "thread-health-01", "2026-06-30T08:12:44",
        "Marijke de Boer <marijke.deboer@ziggo.nl>",
        "Hartkloppingen na inname",
        "Beste Cloudplunge,\n\n"
        "Ik gebruik jullie capsules nu vijf dagen en sinds gisteren heb ik last "
        "van hartkloppingen als ik naar bed ga. Ik voel me er niet lekker bij en "
        "ben er ook een beetje misselijk van. Kan dit door de capsules komen? "
        "Ik durf ze nu even niet meer te nemen.\n\nGroet,\nMarijke de Boer",
    ),
    # 2 — the classic melatonin ingredient question.
    gmail_message(
        "msg-mel-01", "thread-mel-01", "2026-06-29T19:41:02",
        "Sandra Vermeulen <s.vermeulen74@gmail.com>",
        "Vraag over melatonine",
        "Hoi!\n\nIk zag jullie advertentie voorbijkomen. Zit er melatonine in "
        "Cloudplunge? Ik werd altijd zo suf van melatoninetabletten, dus ik hoop "
        "eigenlijk van niet. Wat zit er dan wél in?\n\nGroetjes, Sandra",
    ),
    # 3+4 — delivery complaint that escalates: one thread, two messages.
    gmail_message(
        "msg-lev-01", "thread-lev-01", "2026-06-27T10:03:18",
        "Ingrid Jansen <ingrid.jansen@hotmail.com>",
        "Bestelling #2841 niet ontvangen",
        "Goedemorgen,\n\nOp 20 juni heb ik besteld (ordernummer 2841) en betaald, "
        "maar ik heb nog niets ontvangen. De track & trace doet het niet. "
        "Kunnen jullie uitzoeken waar mijn pakketje is?\n\nMvg, Ingrid Jansen",
    ),
    gmail_message(
        "msg-lev-02", "thread-lev-01", "2026-06-30T16:47:55",
        "Ingrid Jansen <ingrid.jansen@hotmail.com>",
        "Re: Bestelling #2841 niet ontvangen",
        "Inmiddels drie dagen verder en nog steeds niets ontvangen én geen "
        "reactie van jullie. Dit vind ik echt niet kunnen voor zo'n duur product. "
        "Als het pakket er vrijdag niet is wil ik mijn geld terug.\n\nIngrid",
    ),
    # 5 — our own reply in that thread: must be filtered out (outgoing).
    gmail_message(
        "msg-reply-01", "thread-lev-01", "2026-06-30T17:20:00",
        f"Cloudplunge Support <{MAILBOX}>",
        "Re: Bestelling #2841 niet ontvangen",
        "Beste Ingrid, wat vervelend! We zoeken het direct uit en komen er "
        "morgen op terug.\n\nHartelijke groet, Kylian",
    ),
    # 6 — a compliment.
    gmail_message(
        "msg-comp-01", "thread-comp-01", "2026-06-24T21:15:31",
        "Els van Dijk <els.vandijk56@kpnmail.nl>",
        "Eindelijk weer slapen!",
        "Lieve mensen van Cloudplunge,\n\nNa jaren slecht inslapen val ik nu "
        "binnen een kwartier in slaap. Ik was sceptisch maar ben om! Mijn zus "
        "heeft inmiddels ook een potje besteld. Dank jullie wel.\n\nEls (52)",
    ),
    # 7 — usage/dosage question.
    gmail_message(
        "msg-dos-01", "thread-dos-01", "2026-06-21T13:58:09",
        "Petra Willems <petra.willems@outlook.com>",
        "Wanneer innemen?",
        "Hallo,\n\nMoet ik de capsules vlak voor het slapen innemen of eerder op "
        "de avond? En is 1 capsule genoeg of mag ik er 2 nemen?\n\nGroet, Petra",
    ),
    # 8 — medication-interaction question: health flag, even though it is
    # 'just' a question. Doubt = flag.
    gmail_message(
        "msg-med-01", "thread-med-01", "2026-07-01T07:36:12",
        "Anneke Smit <anneke.smit1968@gmail.com>",
        "Gebruik met antidepressiva?",
        "Goedemorgen,\n\nIk gebruik sinds kort antidepressiva (sertraline). "
        "Kan ik Cloudplunge daar veilig naast gebruiken, of zit er iets in dat "
        "een wisselwerking kan geven? Mijn huisarts kende jullie product niet.\n\n"
        "Vriendelijke groet, Anneke Smit",
    ),
    # 9 — newsletter: List-Unsubscribe header → must be filtered out.
    gmail_message(
        "msg-news-01", "thread-news-01", "2026-06-28T06:00:00",
        "Shopify <nieuwsbrief@shopify.com>",
        "Uw weekoverzicht: 12 nieuwe bestellingen",
        "Bekijk uw winkelprestaties van deze week in het dashboard.",
        extra_headers={"List-Unsubscribe": "<https://shopify.com/unsub>",
                       "Precedence": "bulk"},
    ),
    # 10 — return / money-back question.
    gmail_message(
        "msg-retour-01", "thread-retour-01", "2026-06-18T11:22:47",
        "Joke Bakker <joke.bakker@planet.nl>",
        "Retour en geld-terug-garantie",
        "Beste klantenservice,\n\nOp de site staat een geld-terug-garantie. Hoe "
        "werkt dat precies als het product bij mij niet werkt? Moet ik het potje "
        "dan terugsturen, ook als het leeg is?\n\nMet vriendelijke groet, Joke Bakker",
    ),
    # 11 — HTML-only body: exercises the HTML fallback in the parser.
    gmail_message(
        "msg-html-01", "thread-html-01", "2026-06-15T18:05:26",
        "Femke Mulder <femkemulder@icloud.com>",
        "Capsules erg groot",
        "<html><body><p>Hallo,</p><p>Ik vind de capsules eerlijk gezegd "
        "<b>erg groot</b> om door te slikken. Zijn er plannen voor een kleinere "
        "variant of een poedervorm?</p><p>Groet,<br>Femke</p></body></html>",
        html=True,
    ),
    # 12 — follow-up with a quoted reply below it: exercises quote stripping.
    gmail_message(
        "msg-quote-01", "thread-mel-01", "2026-06-30T09:14:33",
        "Sandra Vermeulen <s.vermeulen74@gmail.com>",
        "Re: Vraag over melatonine",
        "Dank voor de snelle reactie! Fijn dat er geen melatonine in zit. "
        "Dan bestel ik vandaag een potje. Werkt de kortingscode WELKOM10 nog?\n\n"
        "Groetjes, Sandra\n\n"
        "Op 29 juni 2026 om 20:15 schreef Cloudplunge Support:\n"
        "> Hoi Sandra, goede vraag! Cloudplunge bevat bewust geen melatonine.\n"
        "> In plaats daarvan gebruiken we een natuurlijke formule.\n",
    ),
]


META_FIXTURE = {
    "page": {"id": PAGE_ID, "name": "Cloudplunge"},
    "posts": [
        {
            "id": f"{PAGE_ID}_875301",
            "message": "Wist je dat Cloudplunge bewust géén melatonine bevat? 🌙 "
                       "Natuurlijk beter slapen, zonder dat suffe gevoel 's ochtends.",
            "created_time": "2026-06-25T09:00:00+0000",
            "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301",
            "comments": {
                "data": [
                    {
                        "id": "875301_101",
                        "message": "Zit er dan wel valeriaan in? Wat is het werkzame stofje precies?",
                        "created_time": "2026-06-25T10:12:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301?comment_id=101",
                        "from": {"name": "Lisa Hendriks", "id": "9210000000000101"},
                    },
                    {
                        "id": "875301_102",
                        "message": "Al mijn derde potje! Ik slaap zóveel beter, echt een aanrader 😴",
                        "created_time": "2026-06-26T20:31:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301?comment_id=102",
                        "from": {"name": "Karin de Groot", "id": "9210000000000102"},
                    },
                    {
                        "id": "875301_103",
                        "message": "€29,95 voor 60 capsules vind ik best prijzig hoor. Komt er nog eens een aanbieding?",
                        "created_time": "2026-06-27T08:44:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301?comment_id=103",
                        "from": {"name": "Thea Kuipers", "id": "9210000000000103"},
                    },
                    {
                        # Our own reply: same id as the page → filtered out.
                        "id": "875301_104",
                        "message": "Goede vraag Lisa! We sturen je een DM met de volledige ingrediëntenlijst. 💙",
                        "created_time": "2026-06-25T11:00:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301?comment_id=104",
                        "from": {"name": "Cloudplunge", "id": PAGE_ID},
                    },
                    {
                        # Possible side effect in a comment — the AI must flag
                        # this even though no hard keyword hits.
                        "id": "875301_105",
                        "message": "Ik werd er juist heel duf en wazig van 's ochtends, na drie dagen gestopt. Iemand anders dat ook?",
                        "created_time": "2026-06-28T22:05:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/875301?comment_id=105",
                        "from": {"name": "Miranda Visser", "id": "9210000000000105"},
                    },
                ]
            },
        },
        {
            "id": f"{PAGE_ID}_881442",
            "message": "Onze nieuwe verpakking is er! Zelfde formule, frisser jasje. ✨",
            "created_time": "2026-06-29T15:30:00+0000",
            "permalink_url": "https://www.facebook.com/cloudplunge/posts/881442",
            "comments": {
                "data": [
                    {
                        "id": "881442_201",
                        "message": "Mooi! Maar mijn bestelling kwam vorige week pas na 8 dagen aan, de communicatie daarover kon echt beter.",
                        "created_time": "2026-06-29T16:02:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/881442?comment_id=201",
                        "from": {"name": "Bianca Peters", "id": "9210000000000201"},
                    },
                    {
                        # Health keyword hit (bloedverdunners): pre-flagged at
                        # store time, before any AI runs.
                        "id": "881442_202",
                        "message": "Kan ik dit veilig combineren met bloedverdunners? Gebruik die voor mijn hart.",
                        "created_time": "2026-06-30T19:18:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/881442?comment_id=202",
                        "from": {"name": "Wilma van Leeuwen", "id": "9210000000000202"},
                    },
                    {
                        "id": "881442_203",
                        "message": "@Sanne Groen dit is misschien iets voor jou!",
                        "created_time": "2026-07-01T12:40:00+0000",
                        "permalink_url": "https://www.facebook.com/cloudplunge/posts/881442?comment_id=203",
                        "from": {"name": "Esther Brouwer", "id": "9210000000000203"},
                    },
                ]
            },
        },
    ],
}


TRUSTPILOT_REVIEWS = {
    "reviews": [
        {"id": "tp-own-001", "stars": 5, "title": "Eindelijk een middel dat écht werkt",
         "text": "Na jaren wisselend slapen probeerde ik Cloudplunge. Binnen een week "
                 "merkte ik verschil: sneller inslapen en niet meer om 4 uur wakker. "
                 "Geen suf gevoel overdag, wat ik bij melatonine wél had.",
         "author": "Els R.", "date": "2026-06-26", "language": "nl",
         "url": "https://nl.trustpilot.com/review/cloudplunge.com"},
        {"id": "tp-own-002", "stars": 4, "title": "Goed product, verzending kan sneller",
         "text": "Het product zelf bevalt goed, ik slaap dieper. Wel duurde de "
                 "levering vijf werkdagen, dat mag anno 2026 echt sneller.",
         "author": "Margriet V.", "date": "2026-06-22", "language": "nl",
         "url": "https://nl.trustpilot.com/review/cloudplunge.com"},
        {"id": "tp-own-003", "stars": 2, "title": "Geen verschil gemerkt",
         "text": "Drie weken trouw elke avond een capsule genomen maar ik merk "
                 "eerlijk gezegd geen enkel verschil met daarvoor. Jammer van het geld.",
         "author": "Sandra K.", "date": "2026-06-19", "language": "nl",
         "url": "https://nl.trustpilot.com/review/cloudplunge.com"},
        {"id": "tp-own-004", "stars": 1, "title": "Retourproces is een drama",
         "text": "Product werkte voor mij niet en toen begon het pas echt: drie mails "
                 "gestuurd over de geld-terug-garantie en pas na een week antwoord. "
                 "Uiteindelijk wel netjes opgelost, maar dit moet beter.",
         "author": "Chantal M.", "date": "2026-06-15", "language": "nl",
         "url": "https://nl.trustpilot.com/review/cloudplunge.com"},
        {"id": "tp-own-005", "stars": 5, "title": "Word uitgerust wakker",
         "text": "Ik gebruik het nu een maand en word echt uitgerust wakker. Ook fijn "
                 "dat het natuurlijk is. Abonnementskorting zou welkom zijn!",
         "author": "Hanneke D.", "date": "2026-06-29", "language": "nl",
         "url": "https://nl.trustpilot.com/review/cloudplunge.com"},
    ]
}

BOL_REVIEWS = {
    "reviews": [
        {"id": "bol-own-001", "stars": 5,
         "text": "Heerlijk product. Ik val sneller in slaap en de capsules zijn goed "
                 "te slikken. Netjes verpakt, snel in huis via bol.",
         "author": "M. de Vries", "date": "2026-06-25", "language": "nl",
         "url": "https://www.bol.com/nl/nl/p/cloudplunge-slaapsupplement/"},
        {"id": "bol-own-002", "stars": 3,
         "text": "Doet wat het belooft maar vind het aan de prijzige kant vergeleken "
                 "met andere slaapsupplementen.",
         "author": "T. Janssen", "date": "2026-06-20", "language": "nl",
         "url": "https://www.bol.com/nl/nl/p/cloudplunge-slaapsupplement/"},
        {"id": "bol-own-003", "stars": 1,
         "text": "Na twee dagen gebruik hoofdpijn gekregen, daarna gestopt. Kan "
                 "toeval zijn maar voor mij dus niets.",
         "author": "W. Verhoeven", "date": "2026-06-28", "language": "nl",
         "url": "https://www.bol.com/nl/nl/p/cloudplunge-slaapsupplement/"},
    ]
}


# Competitor reviews: the manual-import CSV the founders will also use.
COMPETITOR_CSV_ROWS = [
    # kanaal, concurrent, datum, sterren, titel, tekst, auteur, url
    ("trustpilot", "Cloudpillo", "2026-06-27", "2", "Kussen zakt in",
     "Na twee maanden zakt het kussen al helemaal in. Voor deze prijs verwacht "
     "ik echt betere kwaliteit.", "Renate B.",
     "https://nl.trustpilot.com/review/cloudpillo.nl"),
    ("trustpilot", "Cloudpillo", "2026-06-24", "4", "Fijn kussen, snelle levering",
     "Volgende dag al in huis. Slaap er lekker op, al moest ik wel even wennen.",
     "Josée K.", "https://nl.trustpilot.com/review/cloudpillo.nl"),
    ("trustpilot", "Cloudpillo", "2026-06-20", "1", "Retourneren is een drama",
     "Proefperiode klinkt leuk, maar retourneren kostte me drie weken en talloze "
     "mails voordat ik mijn geld terug had.", "Annemiek S.",
     "https://nl.trustpilot.com/review/cloudpillo.nl"),
    ("bol", "8hours", "2026-06-29", "2", "",
     "Geen enkel effect gemerkt na een maand slikken. Zonde van het geld.",
     "P. Bos", "https://www.bol.com/nl/nl/p/8hours-slaapformule/"),
    ("bol", "8hours", "2026-06-23", "2", "",
     "Eerste week leek het te werken, daarna helemaal niets meer. Twijfel of er "
     "wel iets werkzaams in zit.", "K. van der Berg",
     "https://www.bol.com/nl/nl/p/8hours-slaapformule/"),
    ("trustpilot", "8hours", "2026-06-18", "2", "Te duur",
     "Veel te duur voor wat het is. Bij de drogist koop je hetzelfde voor de "
     "helft.", "Dorien W.", "https://nl.trustpilot.com/review/8hours.nl"),
    ("trustpilot", "Zelesta", "2026-06-26", "5", "Topkwaliteit dekbed",
     "Heerlijk dekbed, mooie stof en je merkt dat het kwaliteit is. Aanrader!",
     "Fleur H.", "https://nl.trustpilot.com/review/zelesta.nl"),
    ("trustpilot", "Zelesta", "2026-06-21", "3", "Product goed, levering traag",
     "Het dekbed zelf is prima, maar de levering duurde tien dagen zonder "
     "update. Dat kan beter.", "Nienke P.",
     "https://nl.trustpilot.com/review/zelesta.nl"),
    ("bol", "Evidaplus", "2026-06-25", "1", "",
     "Kreeg er buikpijn van, na drie dagen gestopt. Nooit meer.",
     "H. Smulders", "https://www.bol.com/nl/nl/p/evidaplus-nachtrust/"),
    ("trustpilot", "Cabau", "2026-06-22", "1", "Klantenservice onbereikbaar",
     "Drie keer gemaild over mijn bestelling, nul reactie. Via Instagram word "
     "je ook genegeerd. Echt slechte service.", "Samira E.",
     "https://nl.trustpilot.com/review/cabaulifestyle.com"),
]


def build_competitor_csv() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["kanaal", "concurrent", "datum", "sterren", "titel",
                     "tekst", "auteur", "url"])
    writer.writerows(COMPETITOR_CSV_ROWS)
    return buf.getvalue()


# ── Canned analyses (keyed by external_id, built via the real parsers) ──

# Values mirror exactly what the Claude prompt would return.
CANNED: dict[str, dict] = {
    "msg-health-01": {
        "sentiment": "negative", "type": "complaint", "urgency": "urgent",
        "health_flag": True, "themes": ["bijwerking-gezondheid"],
        "language": "nl", "confidence": 0.97,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-mel-01": {
        "sentiment": "neutral", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["ingredientvraag"],
        "language": "nl", "confidence": 0.95,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-lev-01": {
        "sentiment": "negative", "type": "complaint", "urgency": "normal",
        "health_flag": False, "themes": ["levering-verzending"],
        "language": "nl", "confidence": 0.94,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-lev-02": {
        "sentiment": "negative", "type": "complaint", "urgency": "urgent",
        "health_flag": False, "themes": ["levering-verzending", "retour-geldterug"],
        "language": "nl", "confidence": 0.92,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-comp-01": {
        "sentiment": "positive", "type": "compliment", "urgency": "low",
        "health_flag": False, "themes": ["werking-inslapen"],
        "language": "nl", "confidence": 0.97,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-dos-01": {
        "sentiment": "neutral", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["gebruik-dosering"],
        "language": "nl", "confidence": 0.96,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-med-01": {
        "sentiment": "neutral", "type": "question", "urgency": "urgent",
        "health_flag": True, "themes": ["bijwerking-gezondheid", "ingredientvraag"],
        "language": "nl", "confidence": 0.93,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-retour-01": {
        "sentiment": "neutral", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["retour-geldterug"],
        "language": "nl", "confidence": 0.95,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-html-01": {
        "sentiment": "negative", "type": "suggestion", "urgency": "low",
        "health_flag": False, "themes": ["product-capsule-smaak-verpakking"],
        "language": "nl", "confidence": 0.9,
        "competitor_pro": None, "competitor_con": None,
    },
    "msg-quote-01": {
        "sentiment": "positive", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["prijs-korting"],
        "language": "nl", "confidence": 0.88,
        "competitor_pro": None, "competitor_con": None,
    },
    "875301_101": {
        "sentiment": "neutral", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["ingredientvraag"],
        "language": "nl", "confidence": 0.95,
        "competitor_pro": None, "competitor_con": None,
    },
    "875301_102": {
        "sentiment": "positive", "type": "compliment", "urgency": "low",
        "health_flag": False, "themes": ["werking-doorslapen"],
        "language": "nl", "confidence": 0.96,
        "competitor_pro": None, "competitor_con": None,
    },
    "875301_103": {
        "sentiment": "neutral", "type": "question", "urgency": "normal",
        "health_flag": False, "themes": ["prijs-korting"],
        "language": "nl", "confidence": 0.93,
        "competitor_pro": None, "competitor_con": None,
    },
    "875301_105": {
        "sentiment": "negative", "type": "complaint", "urgency": "urgent",
        "health_flag": True, "themes": ["bijwerking-gezondheid", "werking-herstel"],
        "language": "nl", "confidence": 0.85,
        "competitor_pro": None, "competitor_con": None,
    },
    "881442_201": {
        "sentiment": "negative", "type": "complaint", "urgency": "normal",
        "health_flag": False, "themes": ["levering-verzending", "klantenservice-ervaring"],
        "language": "nl", "confidence": 0.94,
        "competitor_pro": None, "competitor_con": None,
    },
    "881442_202": {
        "sentiment": "neutral", "type": "question", "urgency": "urgent",
        "health_flag": True, "themes": ["bijwerking-gezondheid", "ingredientvraag"],
        "language": "nl", "confidence": 0.96,
        "competitor_pro": None, "competitor_con": None,
    },
    "881442_203": {
        "sentiment": "neutral", "type": "review", "urgency": "low",
        "health_flag": False, "themes": ["overig"],
        "language": "nl", "confidence": 0.7,
        "competitor_pro": None, "competitor_con": None,
    },
    "tp-own-001": {
        "sentiment": "positive", "type": "review", "urgency": "low",
        "health_flag": False, "themes": ["werking-inslapen", "werking-doorslapen"],
        "language": "nl", "confidence": 0.96,
        "competitor_pro": None, "competitor_con": None,
    },
    "tp-own-002": {
        "sentiment": "positive", "type": "review", "urgency": "normal",
        "health_flag": False, "themes": ["werking-doorslapen", "levering-verzending"],
        "language": "nl", "confidence": 0.92,
        "competitor_pro": None, "competitor_con": None,
    },
    "tp-own-003": {
        "sentiment": "negative", "type": "review", "urgency": "normal",
        "health_flag": False, "themes": ["geen-effect"],
        "language": "nl", "confidence": 0.95,
        "competitor_pro": None, "competitor_con": None,
    },
    "tp-own-004": {
        "sentiment": "negative", "type": "review", "urgency": "normal",
        "health_flag": False, "themes": ["retour-geldterug", "klantenservice-ervaring"],
        "language": "nl", "confidence": 0.94,
        "competitor_pro": None, "competitor_con": None,
    },
    "tp-own-005": {
        "sentiment": "positive", "type": "review", "urgency": "low",
        "health_flag": False, "themes": ["werking-herstel", "prijs-korting"],
        "language": "nl", "confidence": 0.93,
        "competitor_pro": None, "competitor_con": None,
    },
    "bol-own-001": {
        "sentiment": "positive", "type": "review", "urgency": "low",
        "health_flag": False, "themes": ["werking-inslapen", "product-capsule-smaak-verpakking"],
        "language": "nl", "confidence": 0.95,
        "competitor_pro": None, "competitor_con": None,
    },
    "bol-own-002": {
        "sentiment": "neutral", "type": "review", "urgency": "normal",
        "health_flag": False, "themes": ["prijs-korting"],
        "language": "nl", "confidence": 0.93,
        "competitor_pro": None, "competitor_con": None,
    },
    "bol-own-003": {
        "sentiment": "negative", "type": "review", "urgency": "urgent",
        "health_flag": True, "themes": ["bijwerking-gezondheid"],
        "language": "nl", "confidence": 0.92,
        "competitor_pro": None, "competitor_con": None,
    },
}

# Canned analyses for the competitor CSV rows, in the same order as
# COMPETITOR_CSV_ROWS (keys are computed content hashes).
CANNED_COMPETITOR: list[dict] = [
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["product-capsule-smaak-verpakking"],
     "language": "nl", "confidence": 0.93,
     "competitor_pro": None,
     "competitor_con": "kussen zakt na twee maanden in, kwaliteit valt tegen"},
    {"sentiment": "positive", "type": "review", "urgency": "low",
     "health_flag": False, "themes": ["levering-verzending"],
     "language": "nl", "confidence": 0.92,
     "competitor_pro": "volgende dag geleverd, slaapt lekker",
     "competitor_con": None},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["retour-geldterug", "klantenservice-ervaring"],
     "language": "nl", "confidence": 0.94,
     "competitor_pro": None,
     "competitor_con": "retourneren duurde drie weken en vele mails"},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["geen-effect"],
     "language": "nl", "confidence": 0.95,
     "competitor_pro": None,
     "competitor_con": "geen enkel effect gemerkt na een maand"},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["geen-effect"],
     "language": "nl", "confidence": 0.9,
     "competitor_pro": None,
     "competitor_con": "effect viel na de eerste week volledig weg"},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["prijs-korting"],
     "language": "nl", "confidence": 0.93,
     "competitor_pro": None,
     "competitor_con": "veel te duur vergeleken met de drogist"},
    {"sentiment": "positive", "type": "review", "urgency": "low",
     "health_flag": False, "themes": ["product-capsule-smaak-verpakking"],
     "language": "nl", "confidence": 0.94,
     "competitor_pro": "mooie stof en merkbare kwaliteit",
     "competitor_con": None},
    {"sentiment": "neutral", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["levering-verzending"],
     "language": "nl", "confidence": 0.91,
     "competitor_pro": "product zelf is prima",
     "competitor_con": "levering duurde tien dagen zonder updates"},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": True, "themes": ["bijwerking-gezondheid"],
     "language": "nl", "confidence": 0.9,
     "competitor_pro": None,
     "competitor_con": "klant kreeg buikpijn en stopte na drie dagen"},
    {"sentiment": "negative", "type": "review", "urgency": "normal",
     "health_flag": False, "themes": ["klantenservice-ervaring"],
     "language": "nl", "confidence": 0.94,
     "competitor_pro": None,
     "competitor_con": "klantenservice reageert niet op mail of Instagram"},
]


def main() -> None:
    (HERE / "gmail_messages.json").write_text(
        json.dumps({"messages": GMAIL_MESSAGES}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (HERE / "meta_comments.json").write_text(
        json.dumps(META_FIXTURE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HERE / "trustpilot_reviews.json").write_text(
        json.dumps(TRUSTPILOT_REVIEWS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HERE / "bol_reviews.json").write_text(
        json.dumps(BOL_REVIEWS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    csv_content = build_competitor_csv()
    (HERE / "competitor_reviews.csv").write_text(csv_content, encoding="utf-8")

    # Compute the competitor external_ids through the *real* CSV parser so
    # the canned keys can never drift from what `pulse demo` stores.
    from pulse.sources.manual import parse_csv

    items = parse_csv(csv_content)
    assert len(items) == len(CANNED_COMPETITOR), (
        f"CSV rijen ({len(items)}) en canned analyses ({len(CANNED_COMPETITOR)}) lopen uiteen"
    )
    canned = dict(CANNED)
    for item, analysis in zip(items, CANNED_COMPETITOR):
        canned[item.external_id] = analysis
    (HERE / "analyses_canned.json").write_text(
        json.dumps(canned, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"OK — fixtures geschreven in {HERE}")
    print(f"  gmail: {len(GMAIL_MESSAGES)} berichten, meta: "
          f"{sum(len(p['comments']['data']) for p in META_FIXTURE['posts'])} reacties, "
          f"trustpilot: {len(TRUSTPILOT_REVIEWS['reviews'])}, bol: {len(BOL_REVIEWS['reviews'])}, "
          f"concurrent-CSV: {len(COMPETITOR_CSV_ROWS)}, canned: {len(canned)}")


if __name__ == "__main__":
    main()
