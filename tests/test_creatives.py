"""creatives: media extraction from snapshot HTML and deduplicated downloads."""

from __future__ import annotations

import hashlib

import httpx

from adscout import creatives, store
from adscout.creatives import _download_one, extract_media

# Snapshot HTML embeds JSON with escaped URLs — exactly what Meta renders.
VIDEO_HTML = (
    '<script>{"video_hd_url":"https:\\/\\/cdn.example.com\\/video\\/v1.mp4",'
    '"video_preview_image_url":"https:\\/\\/cdn.example.com\\/thumb.jpg",'
    '"resized_image_url":"https:\\/\\/cdn.example.com\\/still.jpg"}</script>'
)
CAROUSEL_HTML = (
    '<script>{"cards":[{"resized_image_url":"https:\\/\\/cdn.example.com\\/card1.jpg"},'
    '{"resized_image_url":"https:\\/\\/cdn.example.com\\/card2.jpg"}]}</script>'
)
IMAGE_HTML = (
    '<script>{"original_image_url":'
    '"https:\\/\\/cdn.example.com\\/one.jpg?stp=abc\\u0026sig=def"}</script>'
)


class TestExtractMedia:
    def test_video_format_prefers_hd_and_keeps_thumbnail(self):
        fmt, media = extract_media(VIDEO_HTML)
        assert fmt == "video"
        assert media[0] == ("video", "https://cdn.example.com/video/v1.mp4")
        assert ("video_thumbnail", "https://cdn.example.com/thumb.jpg") in media
        assert ("image", "https://cdn.example.com/still.jpg") in media

    def test_multiple_images_detected_as_carousel(self):
        fmt, media = extract_media(CAROUSEL_HTML)
        assert fmt == "carousel"
        assert media == [
            ("image", "https://cdn.example.com/card1.jpg"),
            ("image", "https://cdn.example.com/card2.jpg"),
        ]

    def test_single_image_with_unicode_escape_unescaped(self):
        fmt, media = extract_media(IMAGE_HTML)
        assert fmt == "image"
        # \/ becomes / and & becomes & — a downloadable URL.
        assert media == [("image", "https://cdn.example.com/one.jpg?stp=abc&sig=def")]

    def test_html_without_media_keys_is_unknown(self):
        fmt, media = extract_media("<html><body>geen media hier</body></html>")
        assert fmt == "unknown"
        assert media == []

    def test_duplicate_urls_extracted_once(self):
        html = (
            '{"resized_image_url":"https:\\/\\/cdn.example.com\\/same.jpg",'
            '"resized_image_url":"https:\\/\\/cdn.example.com\\/same.jpg"}'
        )
        fmt, media = extract_media(html)
        assert fmt == "image"  # one unique image, not a carousel
        assert media == [("image", "https://cdn.example.com/same.jpg")]

    def test_non_http_and_empty_urls_ignored(self):
        html = '{"resized_image_url":"data:image\\/png;base64,AAAA","original_image_url":""}'
        assert extract_media(html) == ("unknown", [])


CONTENT = b"identieke-jpeg-bytes"
SHA = hashlib.sha256(CONTENT).hexdigest()


def _media_client(hits: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, content=CONTENT, headers={"content-type": "image/jpeg"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _insert_ad(conn, ad_id: str) -> None:
    advertiser_id = store.add_advertiser(conn, f"Merk-{ad_id}", None, ["NL"])
    conn.execute(
        """INSERT INTO ads (ad_archive_id, advertiser_id, first_seen, last_seen)
           VALUES (?, ?, '2026-07-01', '2026-07-01')""",
        (ad_id, advertiser_id),
    )
    conn.commit()


class TestDownloadOne:
    def test_dedupes_on_ad_id_and_source_url(self, tmp_db, tmp_path):
        _insert_ad(tmp_db, "A1")
        hits: list[str] = []
        client = _media_client(hits)
        creatives_dir = tmp_path / "creatives"
        url = "https://cdn.example.com/one.jpg"

        assert _download_one(tmp_db, "A1", "image", url, creatives_dir, client) is True
        assert _download_one(tmp_db, "A1", "image", url, creatives_dir, client) is False

        assert hits == [url]  # the duplicate never hit the network
        rows = tmp_db.execute("SELECT * FROM creatives WHERE ad_id = 'A1'").fetchall()
        assert len(rows) == 1
        assert rows[0]["sha256"] == SHA
        assert (creatives_dir / f"{SHA}.jpg").read_bytes() == CONTENT

    def test_identical_content_across_ads_shares_one_file(self, tmp_db, tmp_path):
        _insert_ad(tmp_db, "A1")
        _insert_ad(tmp_db, "A2")
        hits: list[str] = []
        client = _media_client(hits)
        creatives_dir = tmp_path / "creatives"

        assert _download_one(
            tmp_db, "A1", "image", "https://cdn.example.com/a.jpg", creatives_dir, client
        )
        assert _download_one(
            tmp_db, "A2", "image", "https://cdn.example.com/b.jpg", creatives_dir, client
        )

        rows = tmp_db.execute("SELECT * FROM creatives ORDER BY ad_id").fetchall()
        assert [r["ad_id"] for r in rows] == ["A1", "A2"]
        assert rows[0]["sha256"] == rows[1]["sha256"] == SHA
        assert rows[0]["local_path"] == rows[1]["local_path"]
        # Content-addressed: two ads, two DB rows, exactly one file on disk.
        assert [p.name for p in creatives_dir.iterdir()] == [f"{SHA}.jpg"]


def test_fetch_for_ad_sets_format_and_stores_media(tmp_db, tmp_path):
    _insert_ad(tmp_db, "A1")
    snapshot_url = "https://www.facebook.com/ads/archive/render_ad/?id=A1&access_token=T"

    def handler(request: httpx.Request) -> httpx.Response:
        if "render_ad" in str(request.url):
            return httpx.Response(200, text=IMAGE_HTML)
        return httpx.Response(200, content=CONTENT, headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stored = creatives.fetch_for_ad(tmp_db, "A1", snapshot_url, tmp_path / "creatives", client)

    assert stored == 1
    ad = tmp_db.execute("SELECT format FROM ads WHERE ad_archive_id = 'A1'").fetchone()
    assert ad["format"] == "image"
    row = tmp_db.execute("SELECT * FROM creatives WHERE ad_id = 'A1'").fetchone()
    assert row["media_type"] == "image"
    assert row["source_url"] == "https://cdn.example.com/one.jpg?stp=abc&sig=def"
