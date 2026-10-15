"""Tests for building the cost statistics rows."""

from datetime import UTC, date, datetime

import pytest

from custom_components.ista_dk.api import UsageDay
from custom_components.ista_dk.const import MeterKind, PriceComponent
from custom_components.ista_dk.prices import Estimates, PriceEntry, PriceTable
from custom_components.ista_dk.statistics import (
    build_cost_statistics,
    build_statistics,
    cost_statistic_id,
    statistic_ids,
)

from .common import CONS_ID, usage_days

pytestmark = pytest.mark.usefixtures("local_time_zone")


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _water_prices(*periods: tuple[date, date, float]) -> PriceTable:
    return PriceTable(
        PriceEntry(PriceComponent.BRUGSVAND, start, end, price)
        for start, end, price in periods
    )


def test_cost_statistic_ids() -> None:
    """The cost id is the consumption id with a cost suffix."""
    assert cost_statistic_id(CONS_ID, MeterKind.HOT_WATER) == (
        "ista_dk:12345678901_hot_water_cost"
    )
    assert len(statistic_ids(CONS_ID)) == 6
    assert "ista_dk:12345678901_heat_cost" in statistic_ids(CONS_ID)
    assert "ista_dk:12345678901_heat" in statistic_ids(CONS_ID)


def test_cost_is_consumption_times_price_with_a_running_sum() -> None:
    """Each day costs its consumption times the unit price of the day."""
    days = [
        UsageDay(date(2026, 9, 1), 1.0),
        UsageDay(date(2026, 9, 2), 2.5),
        UsageDay(date(2026, 9, 3), 0.5),
    ]
    prices = _water_prices((date(2025, 12, 1), date(2026, 11, 30), 10.0))

    assert build_cost_statistics(days, prices, MeterKind.COLD_WATER) == [
        {"start": _utc(2026, 8, 31, 22), "state": 10.0, "sum": 10.0},
        {"start": _utc(2026, 9, 1, 22), "state": 25.0, "sum": 35.0},
        {"start": _utc(2026, 9, 2, 22), "state": 5.0, "sum": 40.0},
    ]


def test_cost_rows_start_when_the_consumption_rows_do() -> None:
    """Cost and consumption statistics share the same hourly starts."""
    days = usage_days("WaterCold")
    prices = _water_prices((date(2025, 12, 1), date(2026, 11, 30), 10.0))

    cost = build_cost_statistics(days, prices, MeterKind.COLD_WATER)
    consumption = build_statistics(days)

    assert [row["start"] for row in cost] == [row["start"] for row in consumption]
    assert [row["sum"] for row in cost] == pytest.approx(
        [row["sum"] * 10 for row in consumption]
    )


def test_days_without_consumption_value_are_skipped_and_zero_is_kept() -> None:
    """Null days have no row, a day of zero consumption costs zero."""
    days = [
        UsageDay(date(2026, 9, 1), 1.0),
        UsageDay(date(2026, 9, 2), None),
        UsageDay(date(2026, 9, 3), 0.0),
    ]
    prices = _water_prices((date(2025, 12, 1), date(2026, 11, 30), 10.0))

    rows = build_cost_statistics(days, prices, MeterKind.COLD_WATER)

    assert [(row["state"], row["sum"]) for row in rows] == [(10.0, 10.0), (0.0, 10.0)]


def test_no_prices_means_no_rows() -> None:
    """Without a unit price nothing is built."""
    days = usage_days("WaterCold")

    assert build_cost_statistics(days, PriceTable(), MeterKind.COLD_WATER) == []
    water_only = _water_prices((date(2025, 12, 1), date(2026, 11, 30), 10.0))
    assert build_cost_statistics(days, water_only, MeterKind.HOT_WATER) == []


def test_each_day_uses_the_price_of_its_period() -> None:
    """A price change between periods shows in the daily cost."""
    days = [
        UsageDay(date(2026, 8, 31), 1.0),
        UsageDay(date(2026, 9, 1), 1.0),
    ]
    prices = _water_prices(
        (date(2025, 12, 1), date(2026, 8, 31), 10.0),
        (date(2026, 9, 1), date(2026, 11, 30), 20.0),
    )

    rows = build_cost_statistics(days, prices, MeterKind.COLD_WATER)

    assert [(row["state"], row["sum"]) for row in rows] == [(10.0, 10.0), (20.0, 30.0)]


def test_a_new_period_reprices_the_days_after_the_last_settled_one() -> None:
    """The open period uses the newest price until its own statement arrives."""
    days = usage_days("WaterCold")
    before = _water_prices((date(2025, 12, 1), date(2026, 8, 31), 10.0))
    after = _water_prices(
        (date(2025, 12, 1), date(2026, 8, 31), 10.0),
        (date(2026, 9, 1), date(2026, 9, 30), 20.0),
    )

    old_rows = build_cost_statistics(days, before, MeterKind.COLD_WATER)
    new_rows = build_cost_statistics(days, after, MeterKind.COLD_WATER)

    assert [row["start"] for row in new_rows] == [row["start"] for row in old_rows]
    unchanged = [
        old["sum"] == new["sum"] for old, new in zip(old_rows, new_rows, strict=True)
    ]
    assert unchanged == [True] * 5 + [False] * 37
    values = [day.value for day in days if day.value is not None]
    assert new_rows[-1]["sum"] == pytest.approx(
        sum(values[:5]) * 10 + sum(values[5:]) * 20
    )


def test_days_without_a_price_are_skipped() -> None:
    """A day no price covers has no cost row."""
    days = [UsageDay(date(2026, 9, 1), 1.0), UsageDay(date(2026, 9, 20), 1.0)]
    prices = PriceTable(
        [
            PriceEntry(
                PriceComponent.BRUGSVAND, date(2025, 12, 1), date(2026, 8, 31), 10.0
            )
        ],
        Estimates({PriceComponent.VARMT_VAND: 5.0}, date(2026, 9, 10)),
    )

    rows = build_cost_statistics(days, prices, MeterKind.HOT_WATER)

    assert [(row["start"], row["state"]) for row in rows] == [
        (_utc(2026, 9, 19, 22), 15.0)
    ]
