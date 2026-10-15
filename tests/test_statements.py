"""Tests for reading, caching and skipping the settlement statements."""

import asyncio
import logging
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.ista_dk.const import STATEMENTS_URL, MeterKind
from custom_components.ista_dk.statements import extract_text

from .common import (
    DOWNLOAD_BASE,
    StatementDoc,
    create_entry,
    heat_statement,
    mock_ista,
    requests_to,
    setup_integration,
    water_statement,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

PERIOD = ("01-12-2024", "30-11-2025")
OLDER_PERIOD = ("01-12-2023", "30-11-2024")
LOGGER = "custom_components.ista_dk.statements"


def _documents() -> list[StatementDoc]:
    return [
        water_statement("100000001", PERIOD, 40.0),
        heat_statement("100000002", PERIOD, 0.9, 30.0),
    ]


def _downloads(aioclient_mock) -> list[str]:
    return [str(call[1]) for call in requests_to(aioclient_mock, DOWNLOAD_BASE)]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


async def test_statements_are_read_once_and_cached(
    hass: HomeAssistant, aioclient_mock, hass_storage
) -> None:
    """Each PDF is downloaded once, the list is checked on every update."""
    mock_ista(aioclient_mock, statements=_documents())

    entry = await setup_integration(hass)

    prices = entry.runtime_data.data.prices
    assert prices.current(MeterKind.COLD_WATER).price == 40.0
    assert prices.current(MeterKind.HOT_WATER).price == 70.0
    assert prices.current(MeterKind.HEAT).price == 0.9
    assert len(_downloads(aioclient_mock)) == 2
    assert hass_storage[f"ista_dk.{entry.entry_id}"]["data"] == {
        "documents": {
            "100000001": [
                {
                    "component": "brugsvand",
                    "start": "2024-12-01",
                    "end": "2025-11-30",
                    "price": 40.0,
                }
            ],
            "100000002": [
                {
                    "component": "varme",
                    "start": "2024-12-01",
                    "end": "2025-11-30",
                    "price": 0.9,
                },
                {
                    "component": "varmt_vand",
                    "start": "2024-12-01",
                    "end": "2025-11-30",
                    "price": 30.0,
                },
            ],
        }
    }

    await entry.runtime_data.async_refresh()

    assert len(_downloads(aioclient_mock)) == 2
    assert len(requests_to(aioclient_mock, STATEMENTS_URL)) == 2


async def test_pdf_extraction_runs_outside_the_event_loop(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Reading a PDF is blocking work and is done in the executor."""
    in_event_loop: list[bool] = []

    def extract_and_record(pdf: bytes) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            in_event_loop.append(False)
        else:
            in_event_loop.append(True)
        return extract_text(pdf)

    mock_ista(aioclient_mock, statements=_documents())

    with patch(
        "custom_components.ista_dk.statements.extract_text", new=extract_and_record
    ):
        entry = await setup_integration(hass)

    assert in_event_loop == [False, False]
    assert entry.runtime_data.data.prices.current(MeterKind.COLD_WATER).price == 40.0


async def test_cache_is_used_after_a_reload(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A restart does not download the statements again."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.prices.current(MeterKind.HEAT).price == 0.9
    assert len(_downloads(aioclient_mock)) == 2


async def test_new_statement_is_downloaded_on_the_next_update(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Only the document that appeared since the last update is downloaded."""
    mock_ista(aioclient_mock, statements=_documents()[:1])
    entry = await setup_integration(hass)
    assert entry.runtime_data.data.prices.current(MeterKind.HEAT) is None
    assert len(_downloads(aioclient_mock)) == 1

    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, statements=_documents())
    await entry.runtime_data.async_refresh()

    assert entry.runtime_data.data.prices.current(MeterKind.HEAT).price == 0.9
    assert _downloads(aioclient_mock) == [_documents()[1].url]


async def test_only_consumer_statements_are_downloaded(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Forecasts and other document types are left alone."""
    documents = [
        *_documents(),
        StatementDoc("100000003", "Varmeregnskab", PERIOD, account_id="FORECAST"),
        StatementDoc("100000004", "Elregnskab", PERIOD),
    ]
    mock_ista(aioclient_mock, statements=documents)

    await setup_integration(hass)

    assert sorted(_downloads(aioclient_mock)) == sorted(
        document.url for document in documents[:2]
    )


async def test_documents_that_fail_are_skipped_and_retried_quietly(
    hass: HomeAssistant, aioclient_mock, hass_storage, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad document never fails the update and is warned about once."""
    good = water_statement("100000001", PERIOD, 40.0)
    failing = [
        water_statement("100000010", OLDER_PERIOD, 1.0, response={"status": 500}),
        water_statement("100000011", OLDER_PERIOD, 1.0, response={"status": 401}),
        water_statement("100000012", OLDER_PERIOD, 1.0, response={"content": b"junk"}),
        water_statement(
            "100000013",
            OLDER_PERIOD,
            1.0,
            lines=["Informationsside", "Denne service er ikke tilgængelig"],
        ),
        water_statement(
            "100000014", OLDER_PERIOD, 1.0, lines=["A page without any prices"]
        ),
    ]
    mock_ista(aioclient_mock, statements=[good, *failing])

    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.LOADED
    prices = entry.runtime_data.data.prices
    assert prices.newest_end.isoformat() == "2025-11-30"
    assert prices.current(MeterKind.COLD_WATER).price == 40.0
    assert set(hass_storage[f"ista_dk.{entry.entry_id}"]["data"]["documents"]) == {
        "100000001"
    }
    warnings = _warnings(caplog)
    assert len(warnings) == 5
    assert sum("download failed" in warning for warning in warnings) == 2
    assert sum("could not be read" in warning for warning in warnings) == 1
    assert sum("ista has no document" in warning for warning in warnings) == 1
    assert sum("no prices were found" in warning for warning in warnings) == 1
    assert all("Brugsvandsregnskab 01.12.2023 - 30.11.2024" in w for w in warnings)

    downloaded = len(_downloads(aioclient_mock))
    await entry.runtime_data.async_refresh()

    retried = _downloads(aioclient_mock)[downloaded:]
    assert good.url not in retried
    assert {document.url for document in failing} <= set(retried)
    assert len(_warnings(caplog)) == 5


async def test_list_failure_keeps_the_cached_prices(
    hass: HomeAssistant, aioclient_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """If ista cannot list the statements the update still succeeds."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    aioclient_mock.clear_requests()
    mock_ista(aioclient_mock, statements_response={"status": 500})

    await entry.runtime_data.async_refresh()

    assert entry.runtime_data.last_update_success
    assert entry.runtime_data.data.prices.current(MeterKind.HOT_WATER).price == 70.0
    assert len(_warnings(caplog)) == 1
    assert "Could not list the ista statements" in _warnings(caplog)[0]

    await entry.runtime_data.async_refresh()

    assert len(_warnings(caplog)) == 1


async def test_unreadable_cache_entries_are_downloaded_again(
    hass: HomeAssistant, aioclient_mock, hass_storage
) -> None:
    """Cached documents that cannot be read are read from ista again."""
    entry = create_entry()
    hass_storage[f"ista_dk.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"ista_dk.{entry.entry_id}",
        "data": {
            "documents": {
                "100000001": [
                    {
                        "component": "brugsvand",
                        "start": "2024-12-01",
                        "end": "2025-11-30",
                        "price": 40.0,
                    }
                ],
                "100000002": [{"component": "nonsense"}],
            }
        },
    }
    mock_ista(aioclient_mock, statements=_documents())

    await setup_integration(hass, entry)

    assert _downloads(aioclient_mock) == [_documents()[1].url]
    assert entry.runtime_data.data.prices.current(MeterKind.HEAT).price == 0.9


async def test_removing_the_entry_deletes_the_cache(
    hass: HomeAssistant, aioclient_mock, hass_storage
) -> None:
    """The cached statement prices do not outlive the config entry."""
    mock_ista(aioclient_mock, statements=_documents())
    entry = await setup_integration(hass)
    assert f"ista_dk.{entry.entry_id}" in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert f"ista_dk.{entry.entry_id}" not in hass_storage
