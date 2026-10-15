"""Tests for setting up the ista Denmark integration and its sensors."""

from datetime import timedelta

import aiohttp
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ista_dk.const import AUTH_URL, DOMAIN, UPDATE_INTERVAL

from .common import (
    CONS_ID,
    load_json,
    mock_ista,
    requests_to,
    sequence,
    setup_integration,
    usage_url,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.mark.parametrize(
    ("entity_id", "state", "unit", "device_class", "precision"),
    [
        ("sensor.ista_cold_water", "412.75", "m³", "water", 3),
        ("sensor.ista_hot_water", "205.125", "m³", "water", 3),
        ("sensor.ista_heat", "3000.0", "kWh", "energy", 0),
    ],
)
async def test_sensors_report_the_meter_registers(
    hass: HomeAssistant,
    ista_api,
    entity_id: str,
    state: str,
    unit: str,
    device_class: str,
    precision: int,
) -> None:
    """Each meter becomes a total-increasing sensor holding its register."""
    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.LOADED
    sensor = hass.states.get(entity_id)
    assert sensor is not None
    assert sensor.state == state
    assert sensor.attributes["unit_of_measurement"] == unit
    assert sensor.attributes["device_class"] == device_class
    assert sensor.attributes["state_class"] == "total_increasing"
    assert sensor.attributes["reading_date"] == "2026-10-07"

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.options["sensor"]["suggested_display_precision"] == precision


async def test_one_device_per_entry(hass: HomeAssistant, ista_api) -> None:
    """All sensors share one service device and have stable unique ids."""
    entry = await setup_integration(hass)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, CONS_ID)}
    assert devices[0].entry_type is DeviceEntryType.SERVICE

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {entity.unique_id for entity in entities} == {
        f"{CONS_ID}_cold_water",
        f"{CONS_ID}_hot_water",
        f"{CONS_ID}_heat",
    }
    assert {entity.device_id for entity in entities} == {devices[0].id}


async def test_only_present_meters_get_sensors_and_series(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """An account without hot water or heat meters only reads what it has."""
    meters = load_json("meters.json")
    meters["Meters"]["Value"] = [
        row for row in meters["Meters"]["Value"] if row["MeterType"] == "CW"
    ]
    mock_ista(aioclient_mock, meters=meters)

    await setup_integration(hass)

    assert hass.states.get("sensor.ista_cold_water") is not None
    assert hass.states.get("sensor.ista_hot_water") is None
    assert hass.states.get("sensor.ista_heat") is None
    assert len(requests_to(aioclient_mock, usage_url("WaterCold"))) == 1
    assert not requests_to(aioclient_mock, usage_url("WaterHot"))
    assert not requests_to(aioclient_mock, usage_url("Energy"))


async def test_poll_refreshes_the_readings(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """The coordinator polls every six hours, logging in again on the way."""
    mock_ista(aioclient_mock)
    entry = await setup_integration(hass)
    assert entry.runtime_data.update_interval == timedelta(hours=6)
    assert hass.states.get("sensor.ista_cold_water").state == "412.75"

    meters = load_json("meters.json")
    (cold,) = (row for row in meters["Meters"]["Value"] if row["MeterType"] == "CW")
    cold["Last_Meter_Reading"] = 413.5
    cold["Reading_date"] = "08-10-2026"
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, meters=meters)

    freezer.tick(UPDATE_INTERVAL - timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ista_cold_water").state == "412.75"
    assert not aioclient_mock.mock_calls

    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    sensor = hass.states.get("sensor.ista_cold_water")
    assert sensor.state == "413.5"
    assert sensor.attributes["reading_date"] == "2026-10-08"
    assert len(requests_to(aioclient_mock, AUTH_URL)) == 1


async def test_setup_recovers_from_an_expired_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A 401 during the first refresh is handled by logging in again."""
    mock_ista(
        aioclient_mock,
        meters_side_effect=sequence(
            {"status": 401}, {"json": load_json("meters.json")}
        ),
    )

    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert len(requests_to(aioclient_mock, AUTH_URL)) == 2
    assert hass.states.get("sensor.ista_cold_water").state == "412.75"


async def test_setup_with_rejected_login_starts_reauth(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Rejected credentials fail the setup and ask the user to log in again."""
    aioclient_mock.post(AUTH_URL, status=400)

    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    "login_response", [{"status": 503}, {"exc": aiohttp.ClientError("boom")}]
)
async def test_setup_retries_when_ista_is_unreachable(
    hass: HomeAssistant, aioclient_mock, login_response: dict
) -> None:
    """Connection problems leave the entry waiting to retry."""
    aioclient_mock.post(AUTH_URL, **login_response)

    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_unload_entry(hass: HomeAssistant, ista_api) -> None:
    """Unloading removes the polling and makes the sensors unavailable."""
    entry = await setup_integration(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.ista_cold_water").state == STATE_UNAVAILABLE
