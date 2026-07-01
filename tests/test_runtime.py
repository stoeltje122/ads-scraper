"""models.runtime_days and AdRecord.is_active — the winner metric."""

from __future__ import annotations

from datetime import date

from adscout.models import AdRecord, runtime_days


class TestRuntimeDays:
    def test_with_stop_date_uses_stop(self):
        # stop wins over last_seen when both are present
        assert runtime_days("2026-01-01", "2026-01-11", "2026-02-01") == 10

    def test_without_stop_uses_last_seen(self):
        assert runtime_days("2026-01-01", None, "2026-01-31") == 30

    def test_missing_start_returns_none(self):
        assert runtime_days(None, "2026-01-11", "2026-02-01") is None

    def test_no_end_at_all_returns_none(self):
        assert runtime_days("2026-01-01", None, None) is None

    def test_negative_runtime_clamped_to_zero(self):
        assert runtime_days("2026-05-01", "2026-04-01", None) == 0

    def test_same_day_is_zero(self):
        assert runtime_days("2026-05-01", "2026-05-01", None) == 0

    def test_datetime_timestamps_parse_date_part(self):
        assert runtime_days("2026-01-01T08:00:00+0000", "2026-01-04T23:59:59+0000", None) == 3


def _ad(stop: str | None) -> AdRecord:
    return AdRecord(
        ad_archive_id="1",
        page_id="2",
        page_name="Merk",
        ad_creation_time=None,
        ad_delivery_start="2026-01-01",
        ad_delivery_stop=stop,
        snapshot_url=None,
    )


class TestIsActive:
    today = date(2026, 7, 1)

    def test_stop_equal_to_today_is_active(self):
        assert _ad("2026-07-01").is_active(self.today) is True

    def test_stop_before_today_is_inactive(self):
        assert _ad("2026-06-30").is_active(self.today) is False

    def test_stop_after_today_is_active(self):
        assert _ad("2026-07-02").is_active(self.today) is True

    def test_no_stop_is_active(self):
        assert _ad(None).is_active(self.today) is True

    def test_stop_as_full_timestamp_on_today_is_active(self):
        assert _ad("2026-07-01T00:00:00+0000").is_active(self.today) is True
