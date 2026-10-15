"""Tests for the cost statistics and the unit price sensors."""

from collections.abc import Callable
from datetime import date
import logging
from unittest.mock import patch

import pytest

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.ista_dk.api import UsageDay
from custom_components.ista_dk.const import MeterKind
from custom_components.ista_dk.statistics import cost_statistic_id, statistic_id

from .common import (
    CONS_ID,
    create_entry,
    get_statistic_metadata,
    get_statistics,
    heat_statement,
    load_json,
    mock_ista,
    set_usage_value,
    setup_integration,
    usage_days,
    usage_days_from,
    water_statement,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

COLD_COST = cost_statistic_id(CONS_ID, MeterKind.COLD_WATER)
HOT_COST = cost_statistic_id(CONS_ID, MeterKind.HOT_WATER)
HEAT_COST = cost_statistic_id(CONS_ID, MeterKind.HEAT)
ALL_COSTS = {COLD_COST, HOT_COST, HEAT_COST}
ADD_STATISTICS = "custom_components.ista_dk.statistics.async_add_external_statistics"

PERIOD = ("01-12-2024", "30-11-2025")
SETTLED_END = date(2026, 9, 15)
SETTLED_PERIOD = ("01-12-2025", "15-09-2026")
ESTIMATE_AFTER = {"estimate_after": "2026-09-15"}


def _heat_usage() -> list[dict]:
    rows = load_json("usage_Energy.json")
    set_usage_value(rows, "2026-09-10", 10.0)
    set_usage_value(rows, "2026-09-20", 20.0)
    set_usage_value(rows, "2026-10-01", 30.0)
    return rows


def _expected(days: list[UsageDay], price_on: Callable[[date], float]) -> list[float]:
    """Return state and sum of every day's cost, flattened for pytest.approx."""
    flat = []
    total = 0.0
    for day in days:
        if day.value is None:
            continue
        cost = day.value * price_on(day.day)
        total += cost
        flat += [cost, total]
    return flat


def _flat(rows: list[dict[str, float]]) -> list[float]:
    return [value for row in rows for value in (row["state"], row["sum"])]


def _cost_writes(add_statistics) -> dict[str, list[list]]:
    writes: dict[str, list[list]] = {}
    for call in add_statistics.call_args_list:
        statistic = call.args[1]["statistic_id"]
        if statistic.endswith("_cost"):
            writes.setdefault(statistic, []).append(call.args[2])
    return writes


def _documents():
    return [
        water_statement("100000001", PERIOD, 40.0),
        heat_statement("100000002", PERIOD, 0.9, 30.0),
    ]


async def test_cost_statistics_are_imported_for_every_meter(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Each day costs its consumption times the unit prices of the statements."""
    heat = _heat_usage()
    mock_ista(aioclient_mock, usage={"Energy": heat}, statements=_documents())

    await setup_integration(hass)

    cold = await get_statistics(hass, COLD_COST)
    hot = await get_statistics(hass, HOT_COST)
    consumption = await get_statistics(
        hass, statistic_id(CONS_ID, MeterKind.COLD_WATER)
    )
    assert len(cold) == len(hot) == 42
    assert [row["start"] for row in cold] == [row["start"] for row in consumption]
    assert _flat(cold) == pytest.approx(
        _expected(usage_days("WaterCold"), lambda _: 40.0)
    )
    assert _flat(hot) == pytest.approx(
        _expected(usage_days("WaterHot"), lambda _: 70.0)
    )
    heat_rows = await get_statistics(hass, HEAT_COST)
    assert _flat(heat_rows) == pytest.approx(
        _expected(usage_days_from(heat), lambda _: 0.9)
    )
    assert heat_rows[-1]["sum"] == pytest.approx(60 * 0.9)


async def test_cost_statistics_metadata(hass: HomeAssistant, aioclient_mock) -> None:
    """Cost statistics are cumulative, in the Home Assistant currency, no unit class."""
    mock_ista(aioclient_mock, statements=_documents())

    await setup_integration(hass)

    metadata = await get_statistic_metadata(hass, ALL_COSTS)
    assert {
        statistic: (meta["name"], meta["unit_of_measurement"], meta["unit_class"])
        for statistic, meta in metadata.items()
    } == {
        COLD_COST: ("ista Cold water cost", "DKK", None),
        HOT_COST: ("ista Hot water cost", "DKK", None),
        HEAT_COST: ("ista Heat cost", "DKK", None),
    }
    for meta in metadata.values():
        assert meta["source"] == "ista_dk"
        assert meta["has_sum"] is True
        assert meta["mean_type"] is StatisticMeanType.NONE


async def test_no_statements_means_no_cost_statistics_or_price_sensors(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Without a settled statement there is nothing to price with."""
    mock_ista(aioclient_mock)

    await setup_integration(hass)

    assert await get_statistic_metadata(hass, ALL_COSTS) == {}
    for entity_id in (
        "sensor.ista_cold_water_price",
        "sensor.ista_hot_water_price",
        "sensor.ista_heat_price",
    ):
        assert hass.states.get(entity_id) is None


async def test_a_statement_arriving_later_adds_costs_and_price_sensors(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Costs and price sensors appear without a reload once prices are known."""
    mock_ista(aioclient_mock)
    entry = await setup_integration(hass)
    assert hass.states.get("sensor.ista_cold_water_price") is None

    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, statements=_documents())
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ista_cold_water_price").state == "40.0"
    assert hass.states.get("sensor.ista_hot_water_price").state == "70.0"
    assert set(await get_statistic_metadata(hass, ALL_COSTS)) == ALL_COSTS
    assert len(await get_statistics(hass, COLD_COST)) == 42


async def test_price_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    """The sensors show the latest settled unit price and the period it is from."""
    mock_ista(aioclient_mock, statements=_documents())

    entry = await setup_integration(hass)

    registry = er.async_get(hass)
    for entity_id, state, unit, key in (
        ("sensor.ista_cold_water_price", "40.0", "DKK/m³", "cold_water_price"),
        ("sensor.ista_hot_water_price", "70.0", "DKK/m³", "hot_water_price"),
        ("sensor.ista_heat_price", "0.9", "DKK/kWh", "heat_price"),
    ):
        sensor = hass.states.get(entity_id)
        assert sensor.state == state
        assert sensor.attributes["unit_of_measurement"] == unit
        assert sensor.attributes["source"] == "settled"
        assert sensor.attributes["period_start"] == "2024-12-01"
        assert sensor.attributes["period_end"] == "2025-11-30"
        assert "device_class" not in sensor.attributes
        assert "state_class" not in sensor.attributes
        assert registry.async_get(entity_id).unique_id == f"{CONS_ID}_{key}"
        assert registry.async_get(entity_id).config_entry_id == entry.entry_id


async def test_price_sensor_unit_follows_the_currency(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The unit is the currency of Home Assistant per m³ or kWh."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    hass.config.currency = "EUR"

    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.ista_cold_water_price")
    assert state.attributes["unit_of_measurement"] == "EUR/m³"
    state = hass.states.get("sensor.ista_heat_price")
    assert state.attributes["unit_of_measurement"] == "EUR/kWh"


async def test_no_price_sensor_for_a_meter_the_account_does_not_have(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Prices of a kind without a meter get no sensor and no cost series."""
    meters = load_json("meters.json")
    meters["Meters"]["Value"] = [
        row for row in meters["Meters"]["Value"] if row["MeterType"] != "HW"
    ]
    mock_ista(aioclient_mock, meters=meters, statements=_documents())

    await setup_integration(hass)

    assert hass.states.get("sensor.ista_cold_water_price") is not None
    assert hass.states.get("sensor.ista_hot_water_price") is None
    assert HOT_COST not in await get_statistic_metadata(hass, ALL_COSTS)


async def test_new_period_reprices_the_open_period_retroactively(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A statement for days priced with an older price rewrites those rows."""
    first = water_statement("100000001", ("01-12-2025", "31-08-2026"), 10.0)
    second = water_statement("100000002", ("01-09-2026", "30-09-2026"), 20.0)
    mock_ista(aioclient_mock, statements=[first])
    entry = await setup_integration(hass)
    before = await get_statistics(hass, COLD_COST)
    assert _flat(before) == pytest.approx(
        _expected(usage_days("WaterCold"), lambda _: 10.0)
    )

    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, statements=[first, second])
    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await entry.runtime_data.async_refresh()

    def price_on(day: date) -> float:
        return 10.0 if day <= date(2026, 8, 31) else 20.0

    after = await get_statistics(hass, COLD_COST)
    assert _flat(after) == pytest.approx(_expected(usage_days("WaterCold"), price_on))
    assert after[:5] == before[:5]
    writes = _cost_writes(add_statistics)
    (written,) = writes[COLD_COST]
    assert len(written) == 37
    assert written[0]["start"].isoformat() == "2026-08-31T22:00:00+00:00"


async def test_a_new_day_of_consumption_writes_only_that_row(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Unchanged days are not rewritten when the series grows."""
    mock_ista(aioclient_mock, statements=_documents()[:1])
    entry = await setup_integration(hass)
    cold = load_json("usage_WaterCold.json")
    set_usage_value(cold, "2026-10-08", 0.5)
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, usage={"WaterCold": cold}, statements=_documents()[:1])

    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await entry.runtime_data.async_refresh()

    writes = _cost_writes(add_statistics)
    assert list(writes) == [COLD_COST]
    assert [
        (row["state"], row["start"].isoformat()) for row in writes[COLD_COST][0]
    ] == [(20.0, "2026-10-07T22:00:00+00:00")]
    rows = await get_statistics(hass, COLD_COST)
    assert len(rows) == 43
    assert rows[-1]["sum"] == pytest.approx(rows[-2]["sum"] + 20.0)


async def test_unchanged_costs_are_not_written_again(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Polling the same data does not touch the cost statistics."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)

    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await entry.runtime_data.async_refresh()

    assert _cost_writes(add_statistics) == {}


async def test_deleted_cost_statistics_are_restored(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The stored rows, not a remembered state, decide what is written."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    before = await get_statistics(hass, COLD_COST)
    get_instance(hass).async_clear_statistics([COLD_COST])
    assert await get_statistics(hass, COLD_COST) == []

    await entry.runtime_data.async_refresh()

    assert await get_statistics(hass, COLD_COST) == before


async def test_removing_the_entry_deletes_the_cost_statistics(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The cost statistics do not outlive the config entry."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    assert set(await get_statistic_metadata(hass, ALL_COSTS)) == ALL_COSTS

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert await get_statistic_metadata(hass, ALL_COSTS) == {}
    assert await get_statistics(hass, COLD_COST) == []


async def test_estimate_applies_after_the_settled_period_only(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Days after the settled period use the estimate, earlier days do not."""
    statement = water_statement("100000001", SETTLED_PERIOD, 40.0)
    mock_ista(aioclient_mock, statements=[statement])
    entry = create_entry({"estimate_brugsvand": 50.0, **ESTIMATE_AFTER})

    await setup_integration(hass, entry)

    def price_on(day: date) -> float:
        return 40.0 if day <= SETTLED_END else 50.0

    cold = await get_statistics(hass, COLD_COST)
    assert _flat(cold) == pytest.approx(_expected(usage_days("WaterCold"), price_on))
    sensor = hass.states.get("sensor.ista_cold_water_price")
    assert sensor.state == "50.0"
    assert sensor.attributes["source"] == "estimate"
    assert sensor.attributes["period_start"] == "2026-09-16"
    assert sensor.attributes["period_end"] is None


async def test_estimates_expire_when_a_newer_statement_arrives(
    hass: HomeAssistant, aioclient_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """A statement ending after estimate_after replaces the estimates, one warning."""
    settled = water_statement("100000001", SETTLED_PERIOD, 40.0)
    newer = water_statement("100000002", ("16-09-2026", "30-09-2026"), 45.0)
    mock_ista(aioclient_mock, statements=[settled])
    entry = create_entry({"estimate_brugsvand": 50.0, **ESTIMATE_AFTER})
    await setup_integration(hass, entry)
    assert hass.states.get("sensor.ista_cold_water_price").state == "50.0"

    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, statements=[settled, newer])
    await entry.runtime_data.async_refresh()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    def price_on(day: date) -> float:
        return 40.0 if day <= SETTLED_END else 45.0

    cold = await get_statistics(hass, COLD_COST)
    assert _flat(cold) == pytest.approx(_expected(usage_days("WaterCold"), price_on))
    sensor = hass.states.get("sensor.ista_cold_water_price")
    assert sensor.state == "45.0"
    assert sensor.attributes["source"] == "settled"
    assert sensor.attributes["period_start"] == "2026-09-16"
    assert sensor.attributes["period_end"] == "2026-09-30"
    expired = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "estimated prices" in record.getMessage()
    ]
    assert len(expired) == 1
    assert "2026-09-15" in expired[0].getMessage()


async def test_estimates_without_a_date_expire_with_the_first_statement(
    hass: HomeAssistant, aioclient_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Estimates saved before any statement are dropped once a statement exists."""
    mock_ista(aioclient_mock, statements=[water_statement("100000001", PERIOD, 40.0)])
    entry = create_entry({"estimate_brugsvand": 50.0})

    await setup_integration(hass, entry)

    assert hass.states.get("sensor.ista_cold_water_price").state == "40.0"
    expired = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "estimated prices" in record.getMessage()
    ]
    assert len(expired) == 1
    assert "ending after" not in expired[0]


async def test_partial_estimates_leave_the_other_components_settled(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Only the components with an estimate switch away from the settled price."""
    heat = _heat_usage()
    documents = [
        water_statement("100000001", SETTLED_PERIOD, 40.0),
        heat_statement("100000002", SETTLED_PERIOD, 0.9, 30.0),
    ]
    mock_ista(aioclient_mock, usage={"Energy": heat}, statements=documents)
    entry = create_entry({"estimate_varme": 1.5, **ESTIMATE_AFTER})

    await setup_integration(hass, entry)

    def heat_price(day: date) -> float:
        return 0.9 if day <= SETTLED_END else 1.5

    assert _flat(await get_statistics(hass, HEAT_COST)) == pytest.approx(
        _expected(usage_days_from(heat), heat_price)
    )
    assert _flat(await get_statistics(hass, COLD_COST)) == pytest.approx(
        _expected(usage_days("WaterCold"), lambda _: 40.0)
    )
    assert _flat(await get_statistics(hass, HOT_COST)) == pytest.approx(
        _expected(usage_days("WaterHot"), lambda _: 70.0)
    )
    assert hass.states.get("sensor.ista_heat_price").attributes["source"] == "estimate"
    assert (
        hass.states.get("sensor.ista_cold_water_price").attributes["source"]
        == "settled"
    )
    assert (
        hass.states.get("sensor.ista_hot_water_price").attributes["source"] == "settled"
    )


async def test_hot_water_mixes_an_estimated_and_a_settled_component(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Hot water adds the estimated Brugsvand to the settled Varmt vand."""
    documents = [
        water_statement("100000001", SETTLED_PERIOD, 40.0),
        heat_statement("100000002", SETTLED_PERIOD, 0.9, 30.0),
    ]
    mock_ista(aioclient_mock, statements=documents)
    entry = create_entry({"estimate_brugsvand": 50.0, **ESTIMATE_AFTER})

    await setup_integration(hass, entry)

    def hot_price(day: date) -> float:
        return 70.0 if day <= SETTLED_END else 80.0

    assert _flat(await get_statistics(hass, HOT_COST)) == pytest.approx(
        _expected(usage_days("WaterHot"), hot_price)
    )
    sensor = hass.states.get("sensor.ista_hot_water_price")
    assert sensor.state == "80.0"
    assert sensor.attributes["source"] == "estimate"
