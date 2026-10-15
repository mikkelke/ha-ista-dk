"""Download, read and cache the ista settlement statements."""

from __future__ import annotations

from datetime import date
from io import BytesIO
import logging
from typing import Any

from pypdf import PdfReader

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import IstaDkApi, IstaDkApiError, IstaDkAuthError, Statement
from .const import DOMAIN, PriceComponent
from .prices import Estimates, PriceEntry, PriceTable, is_placeholder, parse_statement

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
LIST_KEY = "list"


def extract_text(pdf: bytes) -> str:
    """Return the text of the first page of a PDF."""
    return PdfReader(BytesIO(pdf)).pages[0].extract_text()


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


def _serialize(documents: dict[str, list[PriceEntry]]) -> dict[str, Any]:
    return {
        "documents": {
            document_id: [
                {
                    "component": entry.component.value,
                    "start": entry.start.isoformat(),
                    "end": entry.end.isoformat(),
                    "price": entry.price,
                }
                for entry in entries
            ]
            for document_id, entries in documents.items()
        }
    }


def _deserialize(stored: dict[str, Any] | None) -> dict[str, list[PriceEntry]]:
    documents: dict[str, list[PriceEntry]] = {}
    for document_id, rows in ((stored or {}).get("documents") or {}).items():
        try:
            documents[document_id] = [
                PriceEntry(
                    PriceComponent(row["component"]),
                    date.fromisoformat(row["start"]),
                    date.fromisoformat(row["end"]),
                    float(row["price"]),
                )
                for row in rows
            ]
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("Dropping unreadable cached ista statement %s", document_id)
    return documents


async def async_remove_statement_cache(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the cached statement prices of a config entry."""
    await Store(hass, STORAGE_VERSION, _storage_key(entry_id)).async_remove()


class StatementPrices:
    """The prices of all settled statements, each PDF read once and cached."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, _storage_key(entry_id)
        )
        self._documents: dict[str, list[PriceEntry]] = {}
        self._loaded = False
        self._reported: set[str] = set()
        self._expiry_reported = False

    async def async_load(self) -> None:
        """Load the cached prices from storage."""
        if not self._loaded:
            self._documents = _deserialize(await self._store.async_load())
            self._loaded = True

    def table(self, estimates: Estimates | None = None) -> PriceTable:
        """Return the cached prices as a lookup table."""
        return PriceTable(
            (entry for entries in self._documents.values() for entry in entries),
            estimates,
        )

    async def async_update(self, api: IstaDkApi, estimates: Estimates) -> PriceTable:
        """Read statements that are new since the last update."""
        await self.async_load()
        try:
            statements = await api.async_get_statements()
        except IstaDkApiError as err:
            self._report(LIST_KEY, "Could not list the ista statements: %s", err)
            statements = []
        else:
            self._reported.discard(LIST_KEY)

        added = False
        for statement in statements:
            if statement.document_id in self._documents:
                continue
            if entries := await self._async_read(api, statement):
                self._documents[statement.document_id] = entries
                added = True
        if added:
            await self._store.async_save(_serialize(self._documents))

        table = self.table(estimates)
        if table.estimates_expired and not self._expiry_reported:
            self._expiry_reported = True
            _LOGGER.warning(
                "The estimated prices in the integration options have expired "
                "because a settled ista statement%s has arrived, so they are ignored",
                f" ending after {estimates.after}" if estimates.after else "",
            )
        return table

    async def _async_read(
        self, api: IstaDkApi, statement: Statement
    ) -> list[PriceEntry]:
        try:
            pdf = await api.async_download_statement(statement)
            text = await self._hass.async_add_executor_job(extract_text, pdf)
        except IstaDkAuthError:
            raise
        except IstaDkApiError as err:
            self._report(
                statement.document_id,
                "Skipping ista statement %s, the download failed: %s",
                statement.label,
                err,
            )
            return []
        except Exception as err:
            self._report(
                statement.document_id,
                "Skipping ista statement %s, the PDF could not be read: %r",
                statement.label,
                err,
            )
            _LOGGER.debug("PDF error for %s", statement.label, exc_info=True)
            return []

        if is_placeholder(text):
            self._report(
                statement.document_id,
                "Skipping ista statement %s, ista has no document for it",
                statement.label,
            )
            return []
        if not (entries := parse_statement(text)):
            self._report(
                statement.document_id,
                "Skipping ista statement %s, no prices were found in it",
                statement.label,
            )
            return []
        return entries

    def _report(self, key: str, message: str, *args: Any) -> None:
        level = logging.DEBUG if key in self._reported else logging.WARNING
        self._reported.add(key)
        _LOGGER.log(level, message, *args)
