"""Tests for importing the daily series into the recorder."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ista_dk.const import MeterKind
from custom_components.ista_dk.statistics import build_statistics, statistic_id

from .common import (
    CONS_ID,
    get_statistic_metadata,
    get_statistics,
    load_json,
    mock_ista,
    set_usage_value,
    setup_integration,
    usage_days,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

COLD = statistic_id(CONS_ID, MeterKind.COLD_WATER)
HOT = statistic_id(CONS_ID, MeterKind.HOT_WATER)
HEAT = statistic_id(CONS_ID, MeterKind.HEAT)

ADD_STATISTICS = "custom_components.ista_dk.statistics.async_add_external_statistics"


def _ts(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


async def test_first_setup_imports_the_whole_history(
    hass: HomeAssistant, ista_api
) -> None:
    """Every day with a value is stored for each meter, summed cumulatively."""
    await setup_integration(hass)

    cold = await get_statistics(hass, COLD)
    assert len(cold) == 42
    assert cold[0] == {"start": _ts(2026, 8, 26, 22), "state": 0.12, "sum": 0.12}
    assert cold[-1]["start"] == _ts(2026, 10, 6, 22)
    assert cold[-1]["state"] == 0.06
    assert cold[-1]["sum"] == pytest.approx(4.92)

    hot = await get_statistics(hass, HOT)
    assert len(hot) == 42
    assert hot[-1]["state"] == 0.02
    assert hot[-1]["sum"] == pytest.approx(1.5)

    heat = await get_statistics(hass, HEAT)
    assert len(heat) == 42
    assert all(row["state"] == 0.0 and row["sum"] == 0.0 for row in heat)


async def test_statistics_metadata(hass: HomeAssistant, ista_api) -> None:
    """Statistics are external, cumulative and carry the unit of the meter."""
    await setup_integration(hass)

    metadata = await get_statistic_metadata(hass, {COLD, HOT, HEAT})

    assert {
        statistic: (meta["name"], meta["unit_of_measurement"], meta["unit_class"])
        for statistic, meta in metadata.items()
    } == {
        COLD: ("ista Cold water", "m³", "volume"),
        HOT: ("ista Hot water", "m³", "volume"),
        HEAT: ("ista Heat", "kWh", "energy"),
    }
    for meta in metadata.values():
        assert meta["source"] == "ista_dk"
        assert meta["has_sum"] is True
        assert meta["mean_type"] is StatisticMeanType.NONE


async def test_refresh_appends_only_the_new_days(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """New days are added after the stored ones and continue the stored sum."""
    mock_ista(aioclient_mock)
    entry = await setup_integration(hass)
    before = await get_statistics(hass, COLD)

    cold = load_json("usage_WaterCold.json")
    set_usage_value(cold, "2026-10-08", 0.2)
    set_usage_value(cold, "2026-10-09", 0.3)
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, usage={"WaterCold": cold})

    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await entry.runtime_data.async_refresh()

    after = await get_statistics(hass, COLD)
    assert len(after) == 44
    assert after[:42] == before
    assert [row["start"] for row in after[42:]] == [
        _ts(2026, 10, 7, 22),
        _ts(2026, 10, 8, 22),
    ]
    assert [row["state"] for row in after[42:]] == [0.2, 0.3]
    assert [row["sum"] for row in after[42:]] == pytest.approx(
        [before[-1]["sum"] + 0.2, before[-1]["sum"] + 0.5]
    )
    assert [call.args[1]["statistic_id"] for call in add_statistics.call_args_list] == [
        COLD
    ]
    assert len(add_statistics.call_args.args[2]) == 2


async def test_refresh_without_new_days_writes_nothing(
    hass: HomeAssistant, ista_api
) -> None:
    """Polling the same data again leaves the statistics untouched."""
    entry = await setup_integration(hass)
    before = {
        statistic: await get_statistics(hass, statistic)
        for statistic in (COLD, HOT, HEAT)
    }

    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await entry.runtime_data.async_refresh()

    add_statistics.assert_not_called()
    for statistic, rows in before.items():
        assert await get_statistics(hass, statistic) == rows


async def test_import_continues_from_statistics_already_stored(
    hass: HomeAssistant, ista_api
) -> None:
    """The stored sum, not zero, is the starting point for the missing days."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    days = [day for day in usage_days("WaterCold") if day.value is not None]
    seeded = build_statistics(days[:10], running_sum=1000.0)
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": "ista Cold water",
            "source": "ista_dk",
            "statistic_id": COLD,
            "unit_class": "volume",
            "unit_of_measurement": "m³",
        },
        seeded,
    )
    await async_wait_recording_done(hass)

    with patch(ADD_STATISTICS, wraps=async_add_external_statistics) as add_statistics:
        await setup_integration(hass)

    rows = await get_statistics(hass, COLD)
    assert len(rows) == 42
    assert [row["start"] for row in rows] == [
        row["start"].timestamp() for row in build_statistics(days)
    ]
    assert [row["sum"] for row in rows[:10]] == pytest.approx(
        [row["sum"] for row in seeded]
    )
    assert rows[10]["state"] == days[10].value
    assert rows[10]["sum"] == pytest.approx(seeded[-1]["sum"] + days[10].value)
    assert rows[-1]["sum"] == pytest.approx(1004.92)
    cold_call = next(
        call
        for call in add_statistics.call_args_list
        if call.args[1]["statistic_id"] == COLD
    )
    assert len(cold_call.args[2]) == 32


async def test_removing_the_entry_deletes_its_statistics(
    hass: HomeAssistant, ista_api
) -> None:
    """The imported statistics do not outlive the config entry."""
    entry = await setup_integration(hass)
    assert set(await get_statistic_metadata(hass, {COLD, HOT, HEAT})) == {
        COLD,
        HOT,
        HEAT,
    }

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert await get_statistic_metadata(hass, {COLD, HOT, HEAT}) == {}
    assert await get_statistics(hass, COLD) == []
