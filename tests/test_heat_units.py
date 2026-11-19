"""Tests for heat cost allocators (HCA), which count units instead of kWh."""

from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ista_dk.const import DOMAIN, MeterKind
from custom_components.ista_dk.statistics import cost_statistic_id, statistic_id

from .common import (
    CONS_ID,
    StatementDoc,
    get_statistic_metadata,
    get_statistics,
    heat_statement,
    load_json,
    mock_ista,
    requests_to,
    setup_integration,
    usage_days,
    usage_url,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

HEAT_UNITS = statistic_id(CONS_ID, MeterKind.HEAT_UNITS)
HEAT_UNITS_COST = cost_statistic_id(CONS_ID, MeterKind.HEAT_UNITS)
PERIOD = ("01-12-2025", "30-11-2026")


def _hca(
    reading: float | None,
    reading_date: str | None,
    room: str,
    meter_no: str = "20000010",
) -> dict[str, Any]:
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
        "METER_NO": meter_no,
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
        _hca(0, None, "Bathroom", "20000010"),
        _hca(9449, "13-11-2026", "Room", "20000011"),
        _hca(6639, "12-11-2026", "Room", "20000012"),
        _hca(5626, "13-11-2026", "Kitchen", "20000013"),
        _hca(6920, "13-11-2026", "Living Room", "20000014"),
    ]
    meters["Meters"]["Value"] = rows
    return meters


async def test_heat_units_sensor_is_the_weighted_daily_total(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The sensor is the total of the weighted daily units, not the registers."""
    mock_ista(aioclient_mock, meters=_hca_meters())

    await setup_integration(hass)

    days = [day for day in usage_days("Heat") if day.value is not None]
    state = hass.states.get("sensor.ista_heat_units")
    assert state is not None
    assert float(state.state) == sum(day.value for day in days)
    assert state.attributes["unit_of_measurement"] == "units"
    assert state.attributes["state_class"] == "total_increasing"
    assert "device_class" not in state.attributes
    assert state.attributes["reading_date"] == days[-1].day.isoformat()
    assert hass.states.get("sensor.ista_heat") is None

    rows = await get_statistics(hass, HEAT_UNITS)
    assert float(state.state) == rows[-1]["sum"]


async def test_heat_units_sensor_never_drops_when_old_days_go(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Days that ista leaves out of the series later still count."""
    rows = load_json("usage_Heat.json")
    mock_ista(aioclient_mock, meters=_hca_meters(), usage={"Heat": rows})
    entry = await setup_integration(hass)
    before = float(hass.states.get("sensor.ista_heat_units").state)
    await async_wait_recording_done(hass)

    with_value = [i for i, row in enumerate(rows) if row["value"] is not None]
    newer = rows[with_value[10] :]
    newer[-1] = {**newer[-1], "value": 5}
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, meters=_hca_meters(), usage={"Heat": newer})
    await entry.runtime_data.async_refresh()

    assert float(hass.states.get("sensor.ista_heat_units").state) == before + 5


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


async def test_allocators_are_priced_from_a_varmekontrol_statement(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The units price of a Varmekontrol heat statement prices the units."""
    heat = StatementDoc(
        "100000002",
        "Varmeregnskab",
        PERIOD,
        lines=[
            "VARMEOPGØRELSE",
            "01-12-2025 - 30-11-2026",
            "Varme 66420,71 enheder 0,9028 59.966,33",
            "Varmt vand 219,22 m3 71,9588 15.774,73",
        ],
    )
    mock_ista(aioclient_mock, meters=_hca_meters(), statements=[heat])

    await setup_integration(hass)

    expected = [day.value for day in usage_days("Heat") if day.value is not None]
    rows = await get_statistics(hass, HEAT_UNITS_COST)
    assert [row["state"] for row in rows] == pytest.approx(
        [value * 0.9028 for value in expected]
    )
    state = hass.states.get("sensor.ista_heat_units_price")
    assert state is not None
    assert state.state == "0.9028"
    assert state.attributes["unit_of_measurement"] == "DKK/units"


async def test_a_kwh_price_does_not_price_units(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A heat statement in kWh gives no price for units."""
    mock_ista(
        aioclient_mock,
        meters=_hca_meters(),
        statements=[heat_statement("100000002", PERIOD, 0.9, 30.0)],
    )

    await setup_integration(hass)

    assert await get_statistics(hass, HEAT_UNITS)
    assert await get_statistics(hass, HEAT_UNITS_COST) == []
    assert hass.states.get("sensor.ista_heat_units_price") is None


async def test_meters_of_one_kind_are_summed_and_deactivated_ones_skipped(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Two cold water meters add up; a meter that was taken down does not count."""
    meters = load_json("meters.json")
    rows = meters["Meters"]["Value"]
    cold = next(row for row in rows if row["MeterType"] == "CW")
    rows.append({**cold, "Last_Meter_Reading": 42.305, "METER_NO": "20000004"})
    rows.append(
        {
            **cold,
            "Last_Meter_Reading": 999.0,
            "METER_NO": "20000005",
            "Deactivation_date": "2023-07-28T00:00:00",
        }
    )
    mock_ista(aioclient_mock, meters=meters)

    await setup_integration(hass)

    assert float(hass.states.get("sensor.ista_cold_water").state) == pytest.approx(
        412.75 + 42.305
    )


async def test_varmekontrol_account_gets_every_cost(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A Vandregnskab and a Varmeregnskab in the Varmekontrol layout price all meters."""
    water = StatementDoc(
        "100000001",
        "Vandregnskab",
        PERIOD,
        lines=[
            "BRUGSVANDSOPGØRELSE",
            "01-12-2025 - 30-11-2026",
            "Forbrugsvand 624,44 m3 60,2964 37.651,37",
        ],
    )
    heat = StatementDoc(
        "100000002",
        "Varmeregnskab",
        PERIOD,
        lines=[
            "VARMEOPGØRELSE",
            "01-12-2025 - 30-11-2026",
            "Varme 66420,71 enheder 0,9028 59.966,33",
            "Varmt vand 219,22 m3 71,9588 15.774,73",
        ],
    )
    mock_ista(aioclient_mock, meters=_hca_meters(), statements=[water, heat])

    await setup_integration(hass)

    for kind in (MeterKind.COLD_WATER, MeterKind.HOT_WATER, MeterKind.HEAT_UNITS):
        assert await get_statistics(hass, cost_statistic_id(CONS_ID, kind))
    assert float(hass.states.get("sensor.ista_cold_water_price").state) == 60.2964
    assert float(hass.states.get("sensor.ista_hot_water_price").state) == (
        pytest.approx(60.2964 + 71.9588)
    )


async def test_one_sensor_per_radiator_with_its_raw_register(
    hass: HomeAssistant,
    aioclient_mock,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Every allocator gets a device named after its room and a units sensor."""
    mock_ista(aioclient_mock, meters=_hca_meters())

    entry = await setup_integration(hass)

    expected = {
        "sensor.radiator_bathroom_heat_units": ("0.0", None, "20000010"),
        "sensor.radiator_room_20000011_heat_units": (
            "9449.0",
            "2026-11-13",
            "20000011",
        ),
        "sensor.radiator_room_20000012_heat_units": (
            "6639.0",
            "2026-11-12",
            "20000012",
        ),
        "sensor.radiator_kitchen_heat_units": ("5626.0", "2026-11-13", "20000013"),
        "sensor.radiator_living_room_heat_units": ("6920.0", "2026-11-13", "20000014"),
    }
    account = device_registry.async_get_device_by_identifier(
        (DOMAIN, CONS_ID), entry.entry_id
    )
    assert account is not None
    for entity_id, (value, reading_date, meter_no) in expected.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state == value
        assert state.attributes["unit_of_measurement"] == "units"
        assert state.attributes["state_class"] == "total_increasing"
        assert state.attributes["reading_date"] == reading_date
        assert state.attributes["meter_number"] == meter_no

        registered = entity_registry.async_get(entity_id)
        assert registered.unique_id == f"{CONS_ID}_allocator_{meter_no}"
        device = device_registry.async_get(registered.device_id)
        assert device.serial_number == meter_no
        assert device.model == "Doprimo 3 SoC"
        assert device.via_device_id == account.id

    # The raw registers are unweighted, so the total stays the daily series.
    days = [day for day in usage_days("Heat") if day.value is not None]
    assert float(hass.states.get("sensor.ista_heat_units").state) == sum(
        day.value for day in days
    )


async def test_radiators_follow_the_meter_list(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A new allocator is added on refresh, a removed one becomes unavailable."""
    mock_ista(aioclient_mock, meters=_hca_meters())
    entry = await setup_integration(hass)
    assert hass.states.get("sensor.radiator_hall_heat_units") is None

    meters = _hca_meters()
    rows = meters["Meters"]["Value"]
    rows[:] = [row for row in rows if row.get("METER_NO") != "20000013"]
    rows.append(_hca(12, "14-11-2026", "Hall", "20000015"))
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, meters=meters)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.radiator_hall_heat_units").state == "12.0"
    assert hass.states.get("sensor.radiator_kitchen_heat_units").state == "unavailable"


async def test_no_radiator_sensors_without_allocators(
    hass: HomeAssistant, ista_api, entity_registry: er.EntityRegistry
) -> None:
    """Accounts with a heat meter get no radiator sensors."""
    entry = await setup_integration(hass)

    unique_ids = {
        reg.unique_id
        for reg in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert not any("_allocator_" in unique_id for unique_id in unique_ids)
