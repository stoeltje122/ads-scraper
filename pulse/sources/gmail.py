"""Gmail adapter — support mail via the official Gmail API, readonly scope.

Real mode needs two files under data/gmail/ (see PULSE.md for the
step-by-step Google Cloud walkthrough):
- credentials.json: the OAuth client downloaded from Google Cloud
- token.json: written automatically after the one-time login
  (`pulse verify gmail` opens the browser)

The google-* packages are imported lazily so they are not a hard
dependency (install via requirements-gmail.txt). Fixture mode parses
committed sample messages shaped exactly like the real API response,
through the same parsing code.

Privacy: only what feedback analysis needs is kept. The sender's address
is hashed downstream and never persisted; raw_json holds ids/subject only,
never headers or addresses.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

from pulse.models import FeedbackItem, VerifyResult, utc_now
from pulse.sources.base import CredentialsError, SourceAdapter, SourceError

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
FIXTURE_FILE = "gmail_messages.json"
MAX_BODY_CHARS = 5000

# Senders that are (almost) never customer feedback.
_AUTOMATED_SENDER_RE = re.compile(
    r"no[-_]?reply|donotreply|mailer-daemon|postmaster|notificat|nieuwsbrief|"
    r"newsletter|@bounce|unsubscribe|autoreply",
    re.IGNORECASE,
)

# Common reply-quote markers (Dutch and English mail clients).
_QUOTE_MARKERS = [
    re.compile(r"^Op .{4,80} schreef .{2,120}:\s*$"),
    re.compile(r"^On .{4,80} wrote:\s*$"),
    re.compile(r"^-{2,}\s*(Original Message|Oorspronkelijk bericht)\s*-{2,}", re.IGNORECASE),
    re.compile(r"^(Van|From):\s.+", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
]

_SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fwd?|antw)\s*:\s*)+", re.IGNORECASE)


class GmailAdapter(SourceAdapter):
    type = "gmail"

    @property
    def configured(self) -> bool:
        return self.settings.gmail_credentials_path.exists()

    @property
    def authorized(self) -> bool:
        return self.settings.gmail_token_path.exists()

    # ── Collect ──────────────────────────────────────────────────────

    def collect(self, since: datetime | None) -> list[FeedbackItem]:
        if self.fixture_mode:
            raw_messages = self._fixture_messages()
        else:
            raw_messages = self._api_messages(since)
        items: list[FeedbackItem] = []
        skipped = 0
        for msg in raw_messages:
            item = self.parse_message(msg)
            if item is None:
                skipped += 1
                continue
            if since and item.happened_at:
                try:
                    when = datetime.fromisoformat(item.happened_at)
                except ValueError:
                    when = None
                if when and when < since:
                    continue
            items.append(item)
        logger.info(
            "Gmail: %d berichten opgehaald, %d bruikbaar, %d gefilterd (nieuwsbrief/notificatie/uitgaand)",
            len(raw_messages), len(items), skipped,
        )
        return items

    def _fixture_messages(self) -> list[dict]:
        path = self.fixture_dir / FIXTURE_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("messages", [])

    def _api_messages(self, since: datetime | None) -> list[dict]:
        service = self._service()
        window_start = since or (utc_now() - timedelta(days=self.settings.mail_backfill_days))
        # Gmail's after: takes epoch seconds; -in:chats keeps Hangouts out.
        query = f"in:inbox -in:chats after:{int(window_start.timestamp())}"
        messages: list[dict] = []
        page_token: str | None = None
        try:
            while True:
                resp = (
                    service.users()
                    .messages()
                    .list(userId="me", q=query, maxResults=100, pageToken=page_token)
                    .execute()
                )
                for ref in resp.get("messages", []):
                    messages.append(
                        service.users()
                        .messages()
                        .get(userId="me", id=ref["id"], format="full")
                        .execute()
                    )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:  # googleapiclient raises many types; keep the run alive
            raise SourceError(f"Gmail API-fout: {exc}") from exc
        return messages

    # ── Parsing (shared by fixture and real mode) ────────────────────

    def parse_message(self, msg: dict) -> FeedbackItem | None:
        """Normalize one Gmail message resource; None = filtered out."""
        headers = {
            h["name"].lower(): h["value"]
            for h in (msg.get("payload", {}).get("headers") or [])
        }
        display, address = parseaddr(headers.get("from", ""))
        if self._should_skip(headers, address):
            return None

        text = extract_body_text(msg.get("payload", {}))
        text = strip_quoted_reply(text)[:MAX_BODY_CHARS].strip()
        if not text:
            return None

        subject = _SUBJECT_PREFIX_RE.sub("", headers.get("subject", "")).strip()
        happened_at = self._message_date(msg, headers)
        return FeedbackItem(
            external_id=msg["id"],
            text=f"{subject}\n\n{text}".strip() if subject else text,
            happened_at=happened_at,
            author_display=display or (address.split("@")[0] if address else None),
            author_ref=address or None,
            url=f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
            thread_external_id=msg.get("threadId"),
            thread_subject=subject or None,
            # Privacy: ids only — never headers/addresses in raw_json.
            raw={"id": msg.get("id"), "threadId": msg.get("threadId"),
                 "labelIds": msg.get("labelIds", [])},
        )

    def _should_skip(self, headers: dict[str, str], address: str) -> bool:
        """Filter newsletters, notifications and our own outgoing mail."""
        if address and address.casefold() == self.settings.mailbox.casefold():
            return True  # own replies inside a thread: only *incoming* counts
        if _AUTOMATED_SENDER_RE.search(address or ""):
            return True
        if "list-unsubscribe" in headers or "list-id" in headers:
            return True
        if headers.get("precedence", "").casefold() in ("bulk", "list", "junk"):
            return True
        if headers.get("auto-submitted", "no").casefold() != "no":
            return True
        return False

    @staticmethod
    def _message_date(msg: dict, headers: dict[str, str]) -> str | None:
        internal = msg.get("internalDate")
        if internal:
            try:
                dt = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
                return dt.isoformat(timespec="seconds")
            except (ValueError, OverflowError):
                pass
        try:
            return parsedate_to_datetime(headers["date"]).astimezone(timezone.utc).isoformat(
                timespec="seconds"
            )
        except (KeyError, ValueError, TypeError):
            return None

    # ── Verify / credentials ─────────────────────────────────────────

    def verify(self) -> VerifyResult:
        if self.fixture_mode:
            try:
                n = len(self._fixture_messages())
            except (OSError, ValueError) as exc:
                return VerifyResult(False, f"Fixture-bestand onleesbaar: {exc}")
            return VerifyResult(True, f"Fixture-modus: {n} voorbeeldmails leesbaar.")
        if not self.configured:
            return VerifyResult(
                False,
                "Geen Google-credentials gevonden.",
                [
                    f"Verwacht bestand: {self.settings.gmail_credentials_path}",
                    "Volg de stap-voor-stap in PULSE.md, sectie 'Gmail koppelen'.",
                ],
            )
        try:
            service = self._service(interactive=True)
            profile = service.users().getProfile(userId="me").execute()
        except CredentialsError:
            raise
        except Exception as exc:
            return VerifyResult(False, f"Gmail API-fout: {exc}")
        return VerifyResult(
            True,
            f"Gmail gekoppeld: {profile.get('emailAddress')} "
            f"({profile.get('messagesTotal', '?')} berichten in de mailbox).",
            ["Zet de bron aan met: pulse source activate gmail"],
        )

    def _service(self, interactive: bool = False):
        """Build the Gmail API client. interactive=True may open a browser
        for the one-time OAuth consent (only used by `pulse verify`)."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise CredentialsError(
                "De Google-pakketten zijn niet geïnstalleerd. "
                "Installeer met: pip install -r requirements-gmail.txt"
            ) from exc

        creds = None
        token_path = self.settings.gmail_token_path
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        if not creds or not creds.valid:
            if not interactive:
                raise CredentialsError(
                    "Gmail is nog niet ingelogd. Draai eenmalig: pulse verify gmail "
                    "(er opent een browservenster om toestemming te geven)."
                )
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError as exc:
                raise CredentialsError(
                    "google-auth-oauthlib ontbreekt. "
                    "Installeer met: pip install -r requirements-gmail.txt"
                ) from exc
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.settings.gmail_credentials_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Gmail-token opgeslagen: %s", token_path)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Pure helpers (unit-tested directly) ──────────────────────────────


def extract_body_text(payload: dict) -> str:
    """Best text from a (possibly nested multipart) Gmail payload:
    prefer text/plain, fall back to de-tagged text/html."""
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain
    html = _find_part(payload, "text/html")
    if html:
        return _strip_html(html)
    return ""


def _find_part(payload: dict, mime: str) -> str:
    if payload.get("mimeType") == mime and payload.get("body", {}).get("data"):
        return _decode_b64url(payload["body"]["data"])
    for part in payload.get("parts") or []:
        found = _find_part(part, mime)
        if found:
            return found
    return ""


def _decode_b64url(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"[ \t]{2,}", " ", text)


def strip_quoted_reply(text: str) -> str:
    """Keep only the new content of a mail: cut at the first quote marker,
    drop '>' quoted lines and everything after a signature separator."""
    lines: list[str] = []
    for line in text.splitlines():
        if any(marker.match(line.strip()) for marker in _QUOTE_MARKERS):
            break
        if line.strip() == "--":
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
