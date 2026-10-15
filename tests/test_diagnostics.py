"""Tests for the ista Denmark diagnostics."""

import json

import pytest

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .common import (
    CONS_ID,
    PASSWORD,
    USERNAME,
    create_entry,
    heat_statement,
    mock_ista,
    setup_integration,
    water_statement,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


async def test_diagnostics_redact_credentials(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, ista_api
) -> None:
    """Diagnostics summarize the data and never contain the login."""
    entry = await setup_integration(hass)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"] == {
        CONF_USERNAME: REDACTED,
        CONF_PASSWORD: REDACTED,
    }
    assert diagnostics["meters"] == {
        "cold_water": {"reading": 412.75, "reading_date": "2026-10-07"},
        "hot_water": {"reading": 205.125, "reading_date": "2026-10-07"},
        "heat": {"reading": 3000.0, "reading_date": "2026-10-07"},
    }
    assert diagnostics["usage"]["cold_water"] == {
        "days": 48,
        "days_with_value": 42,
        "first_day": "2020-01-01",
        "last_day_with_value": "2026-10-07",
        "last_value": 0.06,
    }
    serialized = json.dumps(diagnostics)
    for secret in (USERNAME, PASSWORD, CONS_ID):
        assert secret not in serialized


async def test_diagnostics_include_the_price_table(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, aioclient_mock
) -> None:
    """The parsed unit prices and the estimates are part of the diagnostics."""
    period = ("01-12-2024", "30-11-2025")
    mock_ista(
        aioclient_mock,
        statements=[
            water_statement("100000001", period, 40.0),
            heat_statement("100000002", period, 0.9, 30.0),
        ],
    )
    options = {"estimate_varme": 1.1, "estimate_after": "2025-11-30"}
    entry = await setup_integration(hass, create_entry(options))

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    settled = {"start": "2024-12-01", "end": "2025-11-30"}
    assert diagnostics["options"] == options
    assert diagnostics["prices"] == {
        "settled": {
            "brugsvand": [{**settled, "price": 40.0}],
            "varme": [{**settled, "price": 0.9}],
            "varmt_vand": [{**settled, "price": 30.0}],
        },
        "estimates": {"varme": 1.1},
        "estimate_after": "2025-11-30",
        "estimates_expired": False,
    }
    assert USERNAME not in json.dumps(diagnostics)
