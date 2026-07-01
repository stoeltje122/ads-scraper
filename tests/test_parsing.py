"""sources.meta.parse_ad: normalizing raw /ads_archive items."""

from __future__ import annotations

from adscout.sources.meta import parse_ad


def _item(**overrides) -> dict:
    base = {
        "id": "123",
        "page_id": "456",
        "page_name": "Merk",
        "ad_delivery_start_time": "2026-01-01",
    }
    base.update(overrides)
    return base


class TestTextVariants:
    def test_parallel_arrays_of_unequal_length_zip_to_longest(self):
        record = parse_ad(
            _item(
                ad_creative_bodies=["b1", "b2", "b3"],
                ad_creative_link_titles=["t1"],
                ad_creative_link_captions=[],
                ad_creative_link_descriptions=["d1", "d2"],
            )
        )
        assert len(record.texts) == 3
        assert (record.texts[0].body, record.texts[0].title, record.texts[0].caption,
                record.texts[0].description) == ("b1", "t1", None, "d1")
        assert (record.texts[1].body, record.texts[1].title, record.texts[1].caption,
                record.texts[1].description) == ("b2", None, None, "d2")
        assert (record.texts[2].body, record.texts[2].title, record.texts[2].caption,
                record.texts[2].description) == ("b3", None, None, None)

    def test_titles_longer_than_bodies(self):
        record = parse_ad(
            _item(ad_creative_bodies=["b1"], ad_creative_link_titles=["t1", "t2"])
        )
        assert len(record.texts) == 2
        assert record.texts[1].body is None
        assert record.texts[1].title == "t2"

    def test_no_text_arrays_gives_no_variants(self):
        record = parse_ad(_item())
        assert record.texts == []

    def test_none_text_arrays_treated_as_empty(self):
        record = parse_ad(
            _item(
                ad_creative_bodies=None,
                ad_creative_link_titles=None,
                ad_creative_link_captions=None,
                ad_creative_link_descriptions=None,
            )
        )
        assert record.texts == []


class TestMissingAndNoneFields:
    def test_bare_item_defaults(self):
        record = parse_ad({"id": "999"})
        assert record.ad_archive_id == "999"
        assert record.page_id is None
        assert record.page_name is None
        assert record.ad_creation_time is None
        assert record.ad_delivery_start is None
        assert record.ad_delivery_stop is None
        assert record.snapshot_url is None
        assert record.platforms == []
        assert record.languages == []
        assert record.eu_reach is None

    def test_explicit_none_platforms_and_languages(self):
        record = parse_ad(_item(publisher_platforms=None, languages=None))
        assert record.platforms == []
        assert record.languages == []

    def test_raw_payload_kept_untouched(self):
        item = _item(eu_total_reach="garbage")
        record = parse_ad(item)
        assert record.raw is item
        assert record.raw["eu_total_reach"] == "garbage"


class TestEuReachCoercion:
    def test_int_stays_int(self):
        assert parse_ad(_item(eu_total_reach=184233)).eu_reach == 184233

    def test_numeric_string_coerced_to_int(self):
        assert parse_ad(_item(eu_total_reach="184233")).eu_reach == 184233

    def test_garbage_string_becomes_none(self):
        assert parse_ad(_item(eu_total_reach="veel")).eu_reach is None

    def test_garbage_list_becomes_none(self):
        assert parse_ad(_item(eu_total_reach=["1"])).eu_reach is None

    def test_none_stays_none(self):
        assert parse_ad(_item(eu_total_reach=None)).eu_reach is None


class TestIdCoercion:
    def test_int_id_coerced_to_str(self):
        record = parse_ad(_item(id=987654321))
        assert record.ad_archive_id == "987654321"
        assert isinstance(record.ad_archive_id, str)

    def test_missing_id_becomes_empty_string(self):
        # The collector skips records with an empty ad_archive_id.
        assert parse_ad({}).ad_archive_id == ""

    def test_int_page_id_coerced_to_str(self):
        assert parse_ad(_item(page_id=456)).page_id == "456"

    def test_falsy_page_id_becomes_none(self):
        assert parse_ad(_item(page_id="")).page_id is None
