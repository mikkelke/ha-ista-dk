"""Tests for building the daily statistics rows."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from homeassistant.util import dt as dt_util

from custom_components.ista_dk.api import UsageDay
from custom_components.ista_dk.const import MeterKind
from custom_components.ista_dk.statistics import build_statistics, statistic_id

from .common import CONS_ID, usage_days

pytestmark = pytest.mark.usefixtures("local_time_zone")


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_statistic_id() -> None:
    """The id combines the domain, consumer number and meter kind."""
    assert (
        statistic_id(CONS_ID, MeterKind.COLD_WATER) == "ista_dk:12345678901_cold_water"
    )
    assert statistic_id(CONS_ID, MeterKind.HOT_WATER) == "ista_dk:12345678901_hot_water"
    assert statistic_id(CONS_ID, MeterKind.HEAT) == "ista_dk:12345678901_heat"


def test_statistic_id_is_always_valid() -> None:
    """Characters that statistic ids do not allow are replaced."""
    assert statistic_id("AB-12 3", MeterKind.HEAT) == "ista_dk:ab_12_3_heat"


def test_first_import_has_one_row_per_day_with_a_value() -> None:
    """Every day with a value becomes a row starting at local midnight."""
    rows = build_statistics(usage_days("WaterCold"))

    assert len(rows) == 42
    assert rows[0]["start"] == _utc(2026, 8, 26, 22)
    assert rows[0]["state"] == 0.12
    assert rows[0]["sum"] == 0.12
    assert rows[-1]["start"] == _utc(2026, 10, 6, 22)
    assert rows[-1]["state"] == 0.06
    assert rows[-1]["sum"] == pytest.approx(4.92)


def test_sum_is_the_running_total_of_the_days() -> None:
    """Each row's sum is the total of all days up to and including it."""
    days = [day for day in usage_days("WaterHot") if day.value is not None]

    rows = build_statistics(days)

    total = 0.0
    for day, row in zip(days, rows, strict=True):
        total += day.value
        assert row["state"] == day.value
        assert row["sum"] == pytest.approx(total)


def test_days_without_a_value_are_skipped() -> None:
    """Leading and trailing nulls produce no rows."""
    starts = {row["start"] for row in build_statistics(usage_days("WaterCold"))}

    assert not starts & {
        _utc(2019, 12, 31, 23),
        _utc(2026, 10, 7, 22),
        _utc(2026, 10, 8, 22),
        _utc(2026, 10, 9, 22),
    }


def test_null_day_between_values_is_skipped() -> None:
    """A gap in the middle of the series is skipped, not counted as zero."""
    days = [
        UsageDay(date(2026, 9, 1), 1.0),
        UsageDay(date(2026, 9, 2), None),
        UsageDay(date(2026, 9, 3), 2.0),
    ]

    rows = build_statistics(days)

    assert rows == [
        {"start": _utc(2026, 8, 31, 22), "state": 1.0, "sum": 1.0},
        {"start": _utc(2026, 9, 2, 22), "state": 2.0, "sum": 3.0},
    ]


def test_zero_is_a_valid_value() -> None:
    """Days with zero consumption still get a row."""
    rows = build_statistics(usage_days("Energy"))

    assert len(rows) == 42
    assert all(row["state"] == 0.0 and row["sum"] == 0.0 for row in rows)


def test_rows_are_in_date_order() -> None:
    """Input order does not matter."""
    days = [
        UsageDay(date(2026, 9, 3), 3.0),
        UsageDay(date(2026, 9, 1), 1.0),
        UsageDay(date(2026, 9, 2), 2.0),
    ]

    rows = build_statistics(days)

    assert [row["sum"] for row in rows] == [1.0, 3.0, 6.0]
    assert [row["start"] for row in rows] == sorted(row["start"] for row in rows)


def test_incremental_import_continues_the_stored_sum() -> None:
    """Only days after the last stored row are built, continuing its sum."""
    last_stored = _utc(2026, 9, 29, 22)

    rows = build_statistics(
        usage_days("WaterCold"), after=last_stored, running_sum=100.0
    )

    assert [row["start"] for row in rows] == [
        _utc(2026, 9, 30, 22),
        _utc(2026, 10, 1, 22),
        _utc(2026, 10, 2, 22),
        _utc(2026, 10, 3, 22),
        _utc(2026, 10, 4, 22),
        _utc(2026, 10, 5, 22),
        _utc(2026, 10, 6, 22),
    ]
    assert rows[0]["state"] == 0.12
    assert rows[0]["sum"] == pytest.approx(100.12)
    assert rows[-1]["state"] == 0.06
    assert rows[-1]["sum"] == pytest.approx(100.82)


def test_incremental_import_equals_the_tail_of_a_full_import() -> None:
    """Resuming from any stored row gives the same rows as importing everything."""
    days = usage_days("WaterHot")
    full = build_statistics(days)

    for stored in (full[0], full[17], full[-2]):
        resumed = build_statistics(
            days, after=stored["start"], running_sum=stored["sum"]
        )
        tail = full[full.index(stored) + 1 :]
        assert [row["start"] for row in resumed] == [row["start"] for row in tail]
        assert [row["state"] for row in resumed] == [row["state"] for row in tail]
        assert [row["sum"] for row in resumed] == pytest.approx(
            [row["sum"] for row in tail]
        )


def test_incremental_import_without_new_days_builds_nothing() -> None:
    """When the last stored row is the newest day there is nothing to add."""
    days = usage_days("WaterCold")

    assert build_statistics(days, after=_utc(2026, 10, 6, 22), running_sum=4.92) == []


def test_start_follows_the_daylight_saving_offset() -> None:
    """Local midnight maps to 22:00 UTC in summer and 23:00 UTC in winter."""
    days = [UsageDay(date(2026, 10, day), 1.0) for day in (24, 25, 26)]
    days += [UsageDay(date(2027, 3, day), 1.0) for day in (27, 28, 29)]

    starts = [row["start"] for row in build_statistics(days)]

    assert starts == [
        _utc(2026, 10, 23, 22),
        _utc(2026, 10, 24, 22),
        _utc(2026, 10, 25, 23),
        _utc(2027, 3, 26, 23),
        _utc(2027, 3, 27, 23),
        _utc(2027, 3, 28, 22),
    ]


def test_start_uses_the_configured_time_zone() -> None:
    """The day boundary is local midnight of Home Assistant's time zone."""
    dt_util.set_default_time_zone(ZoneInfo("America/New_York"))

    (row,) = build_statistics([UsageDay(date(2026, 10, 7), 1.0)])

    assert row["start"] == _utc(2026, 10, 7, 4)
