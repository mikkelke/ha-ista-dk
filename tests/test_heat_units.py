"""Tests for heat cost allocators (HCA), which count units instead of kWh."""

from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.ista_dk.const import MeterKind
from custom_components.ista_dk.statistics import cost_statistic_id, statistic_id

from .common import (
    CONS_ID,
    get_statistic_metadata,
    get_statistics,
    heat_statement,
    load_json,
    mock_ista,
    requests_to,
    setup_integration,
    usage_days,
    usage_url,
    water_statement,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

HEAT_UNITS = statistic_id(CONS_ID, MeterKind.HEAT_UNITS)
HEAT_UNITS_COST = cost_statistic_id(CONS_ID, MeterKind.HEAT_UNITS)
PERIOD = ("01-12-2025", "30-11-2026")


def _hca(reading: float | None, reading_date: str | None, room: str) -> dict[str, Any]:
    return {
        "Message": " ",
        "Headline": "Varme",
        "MeterText": "Varme",
        "MeterType": "HCA",
        "Unit": "Delinger",
        "INST_NO": 1.0,
        "Last_Meter_Reading": reading,
        "Reading_date": reading_date,
        "Last_Meter_Consumption": 0.0,
        "METER_ID": 80000000010.0,
        "ROOM_DESCR": room,
        "METER_NO": "20000010",
        "METCAT_LABEL": "Doprimo 3 SoC",
        "METTYPE_CODE": None,
        "Deactivation_date": "3000-01-01T00:00:00",
        "Activation_date": "2020-01-01T00:00:00",
    }


def _hca_meters() -> dict[str, Any]:
    """Water meters plus one allocator per radiator instead of a heat meter."""
    meters = load_json("meters.json")
    rows = [row for row in meters["Meters"]["Value"] if row["MeterType"] != "ENERGY"]
    rows += [
        # Floor heating allocator that has never been read.
        _hca(0, None, "Bathroom"),
        _hca(9449, "13-11-2026", "Room"),
        _hca(6639, "12-11-2026", "Room"),
        _hca(5626, "13-11-2026", "Kitchen"),
        _hca(6920, "13-11-2026", "Living Room"),
    ]
    meters["Meters"]["Value"] = rows
    return meters


async def test_allocators_are_summed_into_one_sensor(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """All allocators make up one total-increasing sensor in units."""
    mock_ista(aioclient_mock, meters=_hca_meters())

    await setup_integration(hass)

    state = hass.states.get("sensor.ista_heat_units")
    assert state is not None
    assert state.state == str(9449.0 + 6639 + 5626 + 6920)
    assert state.attributes["unit_of_measurement"] == "units"
    assert state.attributes["state_class"] == "total_increasing"
    assert "device_class" not in state.attributes
    assert state.attributes["reading_date"] == "2026-11-13"
    assert hass.states.get("sensor.ista_heat") is None


async def test_allocator_history_is_imported_from_the_heat_series(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The daily units come from the Heat series, not the Energy series."""
    mock_ista(aioclient_mock, meters=_hca_meters())

    await setup_integration(hass)

    assert len(requests_to(aioclient_mock, usage_url("Heat"))) == 1
    assert not requests_to(aioclient_mock, usage_url("Energy"))

    expected = [day.value for day in usage_days("Heat") if day.value is not None]
    rows = await get_statistics(hass, HEAT_UNITS)
    assert [row["state"] for row in rows] == expected
    assert rows[-1]["sum"] == sum(expected)

    metadata = await get_statistic_metadata(hass, {HEAT_UNITS})
    meta = metadata[HEAT_UNITS]
    assert (meta["name"], meta["unit_of_measurement"], meta["unit_class"]) == (
        "ista Heat units",
        "units",
        None,
    )


async def test_allocators_are_never_priced(hass: HomeAssistant, aioclient_mock) -> None:
    """A heat statement prices kWh only, so units get no cost or price sensor."""
    mock_ista(
        aioclient_mock,
        meters=_hca_meters(),
        statements=[
            water_statement("100000001", PERIOD, 40.0),
            heat_statement("100000002", PERIOD, 0.9, 30.0),
        ],
    )

    await setup_integration(hass)

    assert await get_statistics(hass, HEAT_UNITS)
    assert await get_statistics(hass, HEAT_UNITS_COST) == []
    assert hass.states.get("sensor.ista_cold_water_price") is not None
    assert hass.states.get("sensor.ista_heat_units_price") is None
