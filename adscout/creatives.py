"""Best-effort creative download.

The Ad Library API returns no direct media URLs for commercial ads; the
token-authorized ad_snapshot_url renders the ad and embeds media URLs in
its HTML. We extract those conservatively (regex on known keys), download,
and deduplicate on sha256 — identical files are stored once, so ads sharing
a creative point at the same file.

Everything here is best-effort and non-fatal: when extraction or download
fails, the dashboard falls back to the official Ad Library link.

Compliance: downloaded creatives are copyrighted material of the
competitors — internal analysis only, never republish.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MAX_MEDIA_PER_AD = 12

# Known media keys in the embedded snapshot JSON, in preference order.
_IMAGE_KEYS = ("original_image_url", "resized_image_url")
_VIDEO_KEYS = ("video_hd_url", "video_sd_url", "watermarked_video_hd_url", "watermarked_video_sd_url")
_VIDEO_THUMB_KEYS = ("video_preview_image_url",)

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
}


def _find_urls(html: str, keys: tuple[str, ...]) -> list[str]:
    urls: list[str] = []
    for key in keys:
        for m in re.finditer(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', html):
            try:
                url = json.loads(f'"{m.group(1)}"')  # unescape \/ and \uXXXX
            except ValueError:
                continue
            if url and url.startswith("http") and url not in urls:
                urls.append(url)
    return urls


def extract_media(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (format, [(media_type, url), ...]) from snapshot-render HTML.

    format: image | video | carousel | unknown.
    """
    videos = _find_urls(html, _VIDEO_KEYS)
    video_thumbs = _find_urls(html, _VIDEO_THUMB_KEYS)
    images = _find_urls(html, _IMAGE_KEYS)

    media: list[tuple[str, str]] = []
    if videos:
        media.append(("video", videos[0]))  # best-quality variant only
    media.extend(("video_thumbnail", u) for u in video_thumbs[:1])
    media.extend(("image", u) for u in images[:MAX_MEDIA_PER_AD])

    if videos:
        fmt = "video"
    elif len(images) > 1:
        fmt = "carousel"
    elif images:
        fmt = "image"
    else:
        fmt = "unknown"
    return fmt, media[:MAX_MEDIA_PER_AD]


def fetch_for_ad(
    conn: sqlite3.Connection,
    ad_id: str,
    snapshot_url: str | None,
    creatives_dir: Path,
    client: httpx.Client | None = None,
) -> int:
    """Download media for one ad. Returns number of files stored."""
    if not snapshot_url:
        return 0
    own_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    stored = 0
    try:
        resp = client.get(snapshot_url)
        if resp.status_code != 200:
            logger.info("Snapshot van ad %s niet ophaalbaar (HTTP %d)", ad_id, resp.status_code)
            return 0
        fmt, media = extract_media(resp.text)
        if fmt != "unknown":
            conn.execute("UPDATE ads SET format = ? WHERE ad_archive_id = ?", (fmt, ad_id))
        for media_type, url in media:
            if _download_one(conn, ad_id, media_type, url, creatives_dir, client):
                stored += 1
        conn.commit()
    except httpx.HTTPError as exc:
        logger.info("Creative-download voor ad %s mislukt: %s", ad_id, exc)
    finally:
        if own_client:
            client.close()
    return stored


def _download_one(
    conn: sqlite3.Connection,
    ad_id: str,
    media_type: str,
    url: str,
    creatives_dir: Path,
    client: httpx.Client,
) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM creatives WHERE ad_id = ? AND source_url = ?", (ad_id, url)
    ).fetchone()
    if exists:
        return False
    try:
        resp = client.get(url)
        if resp.status_code != 200 or not resp.content:
            return False
    except httpx.HTTPError:
        return False

    sha = hashlib.sha256(resp.content).hexdigest()
    ext = _EXT_BY_CONTENT_TYPE.get(
        (resp.headers.get("content-type") or "").split(";")[0].strip(),
        ".mp4" if media_type == "video" else ".jpg",
    )
    creatives_dir.mkdir(parents=True, exist_ok=True)
    # Content-addressed: identical creatives across ads share one file.
    local_path = creatives_dir / f"{sha}{ext}"
    if not local_path.exists():
        local_path.write_bytes(resp.content)
    conn.execute(
        """INSERT OR IGNORE INTO creatives
           (ad_id, media_type, source_url, local_path, sha256, downloaded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            ad_id,
            media_type,
            url,
            str(local_path),
            sha,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    return True


def fetch_for_new_ads(
    conn: sqlite3.Connection, ad_ids: list[str], creatives_dir: Path
) -> int:
    """Download creatives for a batch of (new) ads. Never raises."""
    total = 0
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for ad_id in ad_ids:
            row = conn.execute(
                "SELECT snapshot_url FROM ads WHERE ad_archive_id = ?", (ad_id,)
            ).fetchone()
            if not row:
                continue
            try:
                total += fetch_for_ad(conn, ad_id, row["snapshot_url"], creatives_dir, client)
            except Exception:
                logger.exception("Creative-download voor %s mislukt — ga door", ad_id)
    logger.info("Creatives gedownload: %d bestanden voor %d ads", total, len(ad_ids))
    return total
