"""Tests for the ista Denmark config flow."""

from unittest.mock import AsyncMock

import aiohttp
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ista_dk.const import AUTH_URL, DOMAIN

from .common import CONS_ID, PASSWORD, USERNAME, create_entry, load_json

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _start_user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_user_flow_creates_entry(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """A valid login creates an entry keyed by the account's consumer id."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: f"  {USERNAME} ", CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test User"
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    assert result["result"].unique_id == CONS_ID
    assert len(mock_setup_entry.mock_calls) == 1
    assert aioclient_mock.mock_calls[0][2] == {
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }


async def test_user_flow_title_falls_back_to_consumer_number(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """Without a first name the entry is titled after the consumer number."""
    aioclient_mock.post(AUTH_URL, json={**load_json("token.json"), "FirstName": ""})
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"ista {CONS_ID}"


@pytest.mark.parametrize(
    ("login_response", "error"),
    [
        ({"status": 400}, "invalid_auth"),
        ({"status": 401}, "invalid_auth"),
        ({"status": 500}, "cannot_connect"),
        ({"exc": aiohttp.ClientError("boom")}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        ({"json": {}}, "cannot_connect"),
        ({"exc": RuntimeError("bug")}, "unknown"),
    ],
)
async def test_user_flow_errors_and_recovery(
    hass: HomeAssistant,
    aioclient_mock,
    mock_setup_entry: AsyncMock,
    login_response: dict,
    error: str,
) -> None:
    """Each failure shows its error and the form still works afterwards."""
    aioclient_mock.post(AUTH_URL, **login_response)
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert not mock_setup_entry.mock_calls

    aioclient_mock.clear_requests()
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_flow_aborts_for_configured_account(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """The same account cannot be added twice."""
    create_entry().add_to_hass(hass)
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """Reauthentication stores the new password and reloads the entry."""
    entry = create_entry()
    entry.add_to_hass(hass)
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_USERNAME] == USERNAME

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    assert len(mock_setup_entry.mock_calls) == 1


async def test_reauth_shows_errors_and_recovers(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """A wrong password keeps the reauth form open."""
    entry = create_entry()
    entry.add_to_hass(hass)
    aioclient_mock.post(AUTH_URL, status=400)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == PASSWORD

    aioclient_mock.clear_requests()
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_aborts_for_different_account(
    hass: HomeAssistant, aioclient_mock, mock_setup_entry: AsyncMock
) -> None:
    """Logging in to another account during reauth is refused."""
    entry = create_entry()
    entry.add_to_hass(hass)
    aioclient_mock.post(
        AUTH_URL, json={**load_json("token.json"), "ConsId": "99999999999"}
    )

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "99999999999", CONF_PASSWORD: "other"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    assert not mock_setup_entry.mock_calls
