"""Meta Marketing API Insights client: our own spend per campaign per day.

Conservative by design: this feeds a daily batch job, not a realtime
system. Rate limiting: exponential backoff on errors + slowing down when
the X-App-Usage header reports high usage (mirrors adscout/sources/meta.py
— one Meta app, one throttling discipline).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterator

import httpx

from compass.config import Settings
from compass.models import AdSpendRecord, VerifyReport, ams_today, api_amount_to_cents, fmt_eur
from compass.sources.base import AdSpendSource, CredentialsError, SourceError

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5
USAGE_SOFT_LIMIT = 80  # % of app quota; above this we pause between calls

# Meta Graph API error codes
ERROR_TOKEN = 190
RATE_LIMIT_CODES = {4, 17, 32, 613}

INSIGHTS_FIELDS = "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values"

# Meta reports one purchase under several action types at once; picking a
# single type by priority prevents double counting. omni_purchase is Meta's
# own de-duplicated total; the others are fallbacks for accounts that lack it.
PURCHASE_ACTION_PRIORITY = (
    "omni_purchase",
    "purchase",
    "offsite_conversion.fb_pixel_purchase",
)

TOKEN_HELP = (
    "Je META_ACCESS_TOKEN is verlopen of ongeldig (Meta error 190). "
    "Long-lived tokens verlopen na ±60 dagen. Vernieuw het token en zet het "
    "in .env — zie de root-README, sectie 'Token vernieuwen'."
)


class MetaInsightsSource(AdSpendSource):
    """Read-only adapter for the daily campaign insights of our own ad account."""

    name = "meta"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.has_meta():
            raise CredentialsError(
                "Geen META_ACCESS_TOKEN/META_AD_ACCOUNT_ID gevonden in .env. "
                "Zie compass/README.md, sectie 'Meta Marketing API'."
            )
        self.settings = settings
        self.account_id = _normalize_account_id(settings.meta_ad_account_id)
        self.client = client or httpx.Client(timeout=30.0)

    # ── AdSpendSource interface ───────────────────────────────────────

    def fetch_daily_spend(self, since: date, until: date) -> Iterator[AdSpendRecord]:
        """Yield one record per campaign per day for the inclusive window.

        time_increment=1 makes Meta return day rows directly, so Compass
        never has to split multi-day aggregates itself. Insights days are
        account-timezone days (Amsterdam) — exactly the Compass day policy.
        """
        params: dict[str, Any] = {
            "access_token": self.settings.meta_access_token,
            "level": "campaign",
            "time_increment": 1,
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "fields": INSIGHTS_FIELDS,
            "limit": PAGE_SIZE,
        }
        yield from (parse_insight_row(row) for row in self._paginate(params))

    def verify(self) -> VerifyReport:
        """Live check for `compass verify`: account reachable, currency EUR,
        insights flowing incl. Meta's own purchase attribution. Reads only —
        never writes to the database."""
        lines: list[str] = []
        try:
            account = self._get(
                f"{self.settings.graph_base_url}/{self.account_id}",
                {
                    "access_token": self.settings.meta_access_token,
                    "fields": "name,currency,account_status",
                },
            )
            lines.append(f"Advertentieaccount: {account.get('name', '?')} ({self.account_id})")
            currency = account.get("currency", "?")
            if currency == "EUR":
                lines.append("Valuta: EUR.")
            else:
                lines.append(
                    f"LET OP: de valuta van dit account is {currency}, geen EUR — "
                    "de bedragen in Compass kloppen dan niet."
                )
            if account.get("account_status") not in (1, None):
                lines.append(
                    f"Let op: account_status is {account['account_status']} (1 = actief)."
                )
            # The last 7 complete days; today is a partial day.
            today = ams_today()
            rows = list(
                self.fetch_daily_spend(today - timedelta(days=7), today - timedelta(days=1))
            )
            total = sum(row.spend_cents for row in rows)
            lines.append(f"Laatste 7 dagen: {len(rows)} campagne-dagen, {fmt_eur(total)} spend.")
            if any(row.meta_purchases or row.meta_purchase_value_cents for row in rows):
                lines.append("Aankopen en aankoopwaarde komen mee — attributie werkt.")
            elif rows:
                lines.append(
                    "Geen aankopen/aankoopwaarde in de insights (pixel niet gekoppeld?) — "
                    "ROAS 'volgens Meta' blijft dan leeg."
                )
            return VerifyReport(source=self.name, ok=True, mode="live", lines=lines)
        except SourceError as exc:  # incl. CredentialsError mid-run (token verlopen)
            lines.append(f"Fout richting Meta: {exc}")
            return VerifyReport(source=self.name, ok=False, mode="live", lines=lines)

    # ── HTTP plumbing ─────────────────────────────────────────────────

    def _paginate(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        url: str | None = f"{self.settings.graph_base_url}/{self.account_id}/insights"
        request_params: dict[str, Any] | None = params
        while url:
            data = self._get(url, request_params)
            yield from data.get("data", [])
            # paging.next is a complete URL including our params & cursor
            url = data.get("paging", {}).get("next")
            request_params = None

    def _get(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning("Retry %d/%d in %ds", attempt, MAX_RETRIES - 1, wait)
                time.sleep(wait)
            try:
                resp = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = SourceError(f"Netwerkfout richting Meta API: {exc}")
                continue

            self._respect_usage_header(resp)

            if resp.status_code == 200:
                return resp.json()

            code, message = _parse_graph_error(resp)
            if code == ERROR_TOKEN:
                raise CredentialsError(TOKEN_HELP)
            if code in RATE_LIMIT_CODES or resp.status_code >= 500:
                last_error = SourceError(
                    f"Meta API tijdelijk niet beschikbaar (code {code}): {message}"
                )
                continue
            raise SourceError(f"Meta API-fout (code {code}): {message}")
        raise last_error or SourceError("Meta API bleef falen na retries")

    def _respect_usage_header(self, resp: httpx.Response) -> None:
        """Parse X-App-Usage and voluntarily slow down near the quota."""
        header = resp.headers.get("x-app-usage")
        if not header:
            return
        try:
            usage = json.loads(header)
            worst = max(
                usage.get("call_count", 0),
                usage.get("total_time", 0),
                usage.get("total_cputime", 0),
            )
        except (ValueError, TypeError):
            return
        if worst >= USAGE_SOFT_LIMIT:
            pause = 60 if worst >= 95 else 20
            logger.warning("App usage op %d%% — pauzeer %ds", worst, pause)
            time.sleep(pause)


def check_token(settings: Settings) -> tuple[bool, str]:
    """Lightweight token health check (used by `compass status`)."""
    if not settings.meta_access_token:
        return False, "geen META_ACCESS_TOKEN in .env"
    try:
        resp = httpx.get(
            f"{settings.graph_base_url}/me",
            params={"access_token": settings.meta_access_token},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return False, f"netwerkfout richting Meta API: {exc}"
    if resp.status_code == 200:
        data = resp.json()
        who = data.get("name") or data.get("id") or "?"
        return True, f"token geldig (account: {who})"
    code, message = _parse_graph_error(resp)
    if code == ERROR_TOKEN:
        return False, TOKEN_HELP
    return False, f"Meta API-fout (code {code}): {message}"


# ── module helpers ───────────────────────────────────────────────────


def _normalize_account_id(raw: str) -> str:
    """Ads Manager shows the bare account number; the API wants 'act_<id>'.
    Accepting both spares the owners a cryptic Graph error."""
    raw = raw.strip()
    return raw if raw.startswith("act_") else f"act_{raw}"


def _parse_graph_error(resp: httpx.Response) -> tuple[int, str]:
    try:
        err = resp.json().get("error", {})
        return int(err.get("code", resp.status_code)), err.get("message", resp.text[:300])
    except (ValueError, TypeError):
        return resp.status_code, resp.text[:300]


def parse_insight_row(row: dict[str, Any]) -> AdSpendRecord:
    """Normalize one campaign-day insights row into an AdSpendRecord.

    Purchases come from the actions list via PURCHASE_ACTION_PRIORITY: the
    first type present wins, the rest is ignored — Meta lists the same
    conversion under several types, so summing would double-count. Missing
    attribution stays None (≠ 0: 'Meta claims nothing' and 'no pixel data'
    must remain distinguishable on the dashboard).
    """
    return AdSpendRecord(
        day=date.fromisoformat(row["date_start"]),
        campaign_id=str(row.get("campaign_id", "")),
        campaign_name=row.get("campaign_name"),
        spend_cents=api_amount_to_cents(row.get("spend")) or 0,
        impressions=_int_or_none(row.get("impressions")),
        clicks=_int_or_none(row.get("clicks")),
        meta_purchases=_pick_purchase(row.get("actions"), _int_or_none),
        meta_purchase_value_cents=_pick_purchase(row.get("action_values"), api_amount_to_cents),
        raw=row,
    )


def _int_or_none(value: Any) -> int | None:
    """Insights serializes counts as strings ('312', sometimes '9.0')."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return int(float(value))


def _pick_purchase(
    entries: list[dict[str, Any]] | None, parse: Callable[[Any], int | None]
) -> int | None:
    """The purchase entry from an actions/action_values list, by priority."""
    by_type = {entry.get("action_type"): entry.get("value") for entry in entries or []}
    for action_type in PURCHASE_ACTION_PRIORITY:
        if action_type in by_type:
            return parse(by_type[action_type])
    return None
