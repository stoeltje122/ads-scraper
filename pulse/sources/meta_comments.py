"""Meta comments adapter — reactions on own Facebook/Instagram posts & ads.

Official Graph API with a Page access token (pages_read_engagement +
pages_read_user_content); the PULSE.md walkthrough reuses the AdScout
developer app. Only *own* pages: Meta offers no legitimate API for
competitor comments — that channel is manual import, by design.

Fixture mode parses committed sample data shaped like the real Graph
response (posts with nested comments), through the same parsing code.

Privacy: the commenter's platform user id is hashed downstream; raw_json
keeps ids and a post excerpt for context, never the author id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from pulse.config import Settings
from pulse.models import FeedbackItem, VerifyResult, utc_now
from pulse.sources.base import CredentialsError, SourceAdapter, SourceError

logger = logging.getLogger(__name__)

FIXTURE_FILE = "meta_comments.json"

# A new comment can land on an old post; look this far back for posts and
# filter the *comments* on `since` client-side.
POST_LOOKBACK_DAYS = 180
MAX_POST_PAGES = 10

_COMMENT_FIELDS = "id,message,created_time,permalink_url,from{name,id}"
_POST_FIELDS = f"id,message,created_time,permalink_url,comments.limit(100){{{_COMMENT_FIELDS}}}"


class MetaCommentsAdapter(SourceAdapter):
    type = "meta_comments"

    def __init__(self, settings: Settings, fixture_dir=None, client: httpx.Client | None = None):
        super().__init__(settings, fixture_dir=fixture_dir)
        self._client = client or httpx.Client(timeout=30)
        self._fixture_page_id: str | None = None  # set when fixtures load

    @property
    def configured(self) -> bool:
        return bool(self.settings.meta_page_token and self.settings.meta_page_id)

    @property
    def _page_id(self) -> str:
        """Own-page id used to filter our own replies; fixtures carry theirs."""
        return self.settings.meta_page_id or self._fixture_page_id or ""

    # ── Collect ──────────────────────────────────────────────────────

    def collect(self, since: datetime | None) -> list[FeedbackItem]:
        posts = self._fixture_posts() if self.fixture_mode else self._api_posts(since)
        items: list[FeedbackItem] = []
        for post in posts:
            for comment in (post.get("comments") or {}).get("data", []):
                item = self.parse_comment(comment, post)
                if item is None:
                    continue
                if since and item.happened_at:
                    try:
                        if datetime.fromisoformat(item.happened_at) < since:
                            continue
                    except ValueError:
                        pass
                items.append(item)
        logger.info("Meta: %d reacties uit %d posts", len(items), len(posts))
        return items

    def _fixture_posts(self) -> list[dict]:
        path = self.fixture_dir / FIXTURE_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._fixture_page_id = (payload.get("page") or {}).get("id")
        return payload.get("posts", [])

    def _api_posts(self, since: datetime | None) -> list[dict]:
        if not self.configured:
            raise CredentialsError(
                "Geen META_PAGE_TOKEN of PULSE_META_PAGE_ID in .env — "
                "zie PULSE.md, sectie 'Facebook/Instagram-reacties koppelen'."
            )
        window = (since or utc_now()) - timedelta(days=POST_LOOKBACK_DAYS)
        posts: list[dict] = []
        # /published_posts covers regular posts; /ads_posts covers ad ("dark")
        # posts. The second edge may fail on missing permissions — that is a
        # warning, not a dead run.
        for edge, required in (("published_posts", True), ("ads_posts", False)):
            try:
                posts.extend(self._fetch_edge(edge, window))
            except SourceError:
                if required:
                    raise
                logger.warning("Meta: %s-edge niet beschikbaar (ads-reacties overgeslagen)", edge)
        return posts

    def _fetch_edge(self, edge: str, window_start: datetime) -> list[dict]:
        url = f"{self.settings.graph_base_url}/{self.settings.meta_page_id}/{edge}"
        params: dict = {
            "fields": _POST_FIELDS,
            "since": int(window_start.timestamp()),
            "limit": 25,
            "access_token": self.settings.meta_page_token,
        }
        posts: list[dict] = []
        for _ in range(MAX_POST_PAGES):
            data = self._get(url, params)
            posts.extend(data.get("data", []))
            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break
            url, params = next_url, {}  # next already carries the query string
        # Follow nested comment paging so busy posts are complete.
        for post in posts:
            comments = post.get("comments") or {}
            next_url = (comments.get("paging") or {}).get("next")
            while next_url:
                data = self._get(next_url, {})
                comments.setdefault("data", []).extend(data.get("data", []))
                next_url = (data.get("paging") or {}).get("next")
        return posts

    def _get(self, url: str, params: dict) -> dict:
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"Meta API onbereikbaar: {exc}") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or "error" in body:
            error = (body.get("error") or {})
            if error.get("code") == 190:
                raise CredentialsError(
                    "Meta Page-token verlopen of ongeldig (fout 190). "
                    "Genereer een nieuwe: zie PULSE.md, sectie 'Page-token vernieuwen'."
                )
            raise SourceError(
                f"Meta API-fout {resp.status_code}: {error.get('message', resp.text[:200])}"
            )
        return body

    # ── Parsing (shared by fixture and real mode) ────────────────────

    def parse_comment(self, comment: dict, post: dict) -> FeedbackItem | None:
        """Normalize one comment; None = filtered out (own replies, empty)."""
        text = (comment.get("message") or "").strip()
        if not text:
            return None  # sticker/GIF-only comments carry no analyzable text
        author = comment.get("from") or {}
        if author.get("id") and str(author["id"]) == str(self._page_id):
            return None  # our own replies under the post
        return FeedbackItem(
            external_id=comment["id"],
            text=text,
            happened_at=_normalize_time(comment.get("created_time")),
            author_display=author.get("name"),
            author_ref=str(author["id"]) if author.get("id") else None,
            url=comment.get("permalink_url") or post.get("permalink_url"),
            # Privacy: ids + post excerpt for context, never the author id.
            raw={
                "comment_id": comment.get("id"),
                "post_id": post.get("id"),
                "post_excerpt": (post.get("message") or "")[:200],
            },
        )

    # ── Verify ───────────────────────────────────────────────────────

    def verify(self) -> VerifyResult:
        if self.fixture_mode:
            try:
                posts = self._fixture_posts()
                n = sum(len((p.get("comments") or {}).get("data", [])) for p in posts)
            except (OSError, ValueError) as exc:
                return VerifyResult(False, f"Fixture-bestand onleesbaar: {exc}")
            return VerifyResult(True, f"Fixture-modus: {n} voorbeeldreacties in {len(posts)} posts.")
        if not self.configured:
            return VerifyResult(
                False,
                "Geen META_PAGE_TOKEN of PULSE_META_PAGE_ID in .env.",
                ["Zie PULSE.md, sectie 'Facebook/Instagram-reacties koppelen'."],
            )
        try:
            page = self._get(
                f"{self.settings.graph_base_url}/{self.settings.meta_page_id}",
                {"fields": "id,name", "access_token": self.settings.meta_page_token},
            )
        except CredentialsError as exc:
            return VerifyResult(False, str(exc))
        except SourceError as exc:
            return VerifyResult(False, str(exc))
        return VerifyResult(
            True,
            f"Pagina gekoppeld: {page.get('name')} (id {page.get('id')}).",
            ["Zet de bron aan met: pulse source activate meta_comments"],
        )


def _normalize_time(value: str | None) -> str | None:
    """Graph timestamps ('2026-06-28T19:04:11+0000') → ISO with colon."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
    except ValueError:
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(
                timespec="seconds"
            )
        except ValueError:
            return value
