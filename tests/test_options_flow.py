"""Tests for the estimated prices options flow."""

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ista_dk.const import MeterKind
from custom_components.ista_dk.statistics import cost_statistic_id

from .common import (
    CONS_ID,
    DOWNLOAD_BASE,
    create_entry,
    get_statistics,
    heat_statement,
    mock_ista,
    requests_to,
    setup_integration,
    usage_days,
    water_statement,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

COLD_COST = cost_statistic_id(CONS_ID, MeterKind.COLD_WATER)
PERIOD = ("01-12-2024", "30-11-2025")


def _documents():
    return [
        water_statement("100000001", PERIOD, 40.0),
        heat_statement("100000002", PERIOD, 0.9, 30.0),
    ]


def _suggested_values(result) -> dict[str, float]:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def _save(hass: HomeAssistant, entry_id: str, user_input: dict) -> None:
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_saving_estimates_stores_the_date_of_the_newest_statement(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The estimates are saved with the end of the newest settled period."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data
    downloads = len(requests_to(aioclient_mock, DOWNLOAD_BASE))

    await _save(hass, entry.entry_id, {"estimate_brugsvand": 50, "estimate_varme": 1.1})

    assert entry.options == {
        "estimate_brugsvand": 50.0,
        "estimate_varme": 1.1,
        "estimate_after": "2025-11-30",
    }
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
    assert len(requests_to(aioclient_mock, DOWNLOAD_BASE)) == downloads

    sensor = hass.states.get("sensor.ista_cold_water_price")
    assert sensor.state == "50.0"
    assert sensor.attributes["source"] == "estimate"
    assert sensor.attributes["period_start"] == "2025-12-01"
    assert hass.states.get("sensor.ista_heat_price").state == "1.1"
    assert (
        hass.states.get("sensor.ista_hot_water_price").attributes["source"]
        == "estimate"
    )
    cold = await get_statistics(hass, COLD_COST)
    consumption = sum(
        day.value for day in usage_days("WaterCold") if day.value is not None
    )
    assert cold[-1]["sum"] == pytest.approx(consumption * 50.0)


async def test_changed_estimates_reprice_the_cost_series(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Saving different estimates rewrites the cost rows after the settled period."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    consumption = sum(
        day.value for day in usage_days("WaterCold") if day.value is not None
    )
    assert (await get_statistics(hass, COLD_COST))[-1]["sum"] == pytest.approx(
        consumption * 40.0
    )

    await _save(hass, entry.entry_id, {"estimate_brugsvand": 60.0})

    assert (await get_statistics(hass, COLD_COST))[-1]["sum"] == pytest.approx(
        consumption * 60.0
    )


async def test_the_form_shows_the_saved_estimates(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Existing estimates are suggested when the form is opened again."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = create_entry({"estimate_brugsvand": 50.0, "estimate_after": "2025-11-30"})
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert _suggested_values(result) == {"estimate_brugsvand": 50.0}
    assert set(result["data_schema"].schema) == {
        "estimate_brugsvand",
        "estimate_varmt_vand",
        "estimate_varme",
    }


async def test_empty_form_clears_the_estimates(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Leaving every field empty removes the estimates and the stored date."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = create_entry({"estimate_brugsvand": 50.0, "estimate_after": "2025-11-30"})
    await setup_integration(hass, entry)
    assert hass.states.get("sensor.ista_cold_water_price").state == "50.0"

    await _save(hass, entry.entry_id, {})

    assert entry.options == {}
    sensor = hass.states.get("sensor.ista_cold_water_price")
    assert sensor.state == "40.0"
    assert sensor.attributes["source"] == "settled"


async def test_estimates_before_the_first_statement_have_no_date(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Without settled statements the estimates price every day."""
    mock_ista(aioclient_mock)
    entry = await setup_integration(hass)
    assert hass.states.get("sensor.ista_cold_water_price") is None

    await _save(hass, entry.entry_id, {"estimate_brugsvand": 50.0})

    assert entry.options == {"estimate_brugsvand": 50.0}
    sensor = hass.states.get("sensor.ista_cold_water_price")
    assert sensor.state == "50.0"
    assert sensor.attributes["source"] == "estimate"
    assert sensor.attributes["period_start"] is None
    cold = await get_statistics(hass, COLD_COST)
    assert len(cold) == 42
    assert cold[0]["state"] == pytest.approx(usage_days("WaterCold")[3].value * 50.0)
    assert hass.states.get("sensor.ista_hot_water_price") is None


async def test_estimate_needs_the_whole_price_of_a_kind(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Hot water has no price while only one of its components is known."""
    mock_ista(aioclient_mock)
    entry = await setup_integration(hass)

    await _save(hass, entry.entry_id, {"estimate_varmt_vand": 30.0})

    assert hass.states.get("sensor.ista_hot_water_price") is None
    assert hass.states.get("sensor.ista_cold_water_price") is None
    assert entry.options == {"estimate_varmt_vand": 30.0}
