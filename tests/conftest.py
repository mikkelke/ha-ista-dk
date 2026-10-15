"""Fixtures for ista Denmark tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.util import dt as dt_util

from .common import mock_ista


@pytest.fixture
def local_time_zone() -> Generator[None]:
    """Use Europe/Copenhagen as the default time zone without a hass instance."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Copenhagen"))
    yield
    dt_util.set_default_time_zone(original)


@pytest.fixture
def ista_api(aioclient_mock):
    """Mock every ista endpoint with the anonymized fixtures."""
    mock_ista(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stop config flow tests from setting the entry up."""
    with patch(
        "custom_components.ista_dk.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup
