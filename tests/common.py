"""Helpers shared by the ista Denmark tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.ista_dk.api import UsageDay
from custom_components.ista_dk.const import (
    AUTH_URL,
    DOMAIN,
    METERS_URL,
    STATEMENT_ACCOUNT_ID,
    STATEMENT_URL,
    STATEMENTS_URL,
    USAGE_SERIES,
    USAGE_URL,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"

CONS_ID = "12345678901"
USERNAME = "12345678901"
PASSWORD = "correct-horse"
WORKS_ID = "90000000010"
INST_ID = "90000000011"
DOWNLOAD_BASE = "https://prod.istaonlinebeta.dk/api/Download/"


def load_json(name: str) -> Any:
    """Load an anonymized API response from tests/fixtures."""
    return json.loads((FIXTURE_DIR / name).read_text())


def usage_url(series: str) -> str:
    """Return the daily-usage URL of a series such as WaterCold."""
    return USAGE_URL.format(series=series)


def usage_days_from(rows: list[dict[str, Any]]) -> list[UsageDay]:
    """Return the days the integration parses from the rows of a series."""
    return [
        UsageDay(day=datetime.fromisoformat(row["date"]).date(), value=row["value"])
        for row in rows
    ]


def usage_days(series: str) -> list[UsageDay]:
    """Return the fixture series as the days the integration parses from it."""
    return usage_days_from(load_json(f"usage_{series}.json"))


def set_usage_value(rows: list[dict[str, Any]], day: str, value: float) -> None:
    """Fill in the value of one day (YYYY-MM-DD) of a fixture series."""
    (row,) = (row for row in rows if row["date"].startswith(day))
    row["value"] = value


def danish(number: float, decimals: int = 7) -> str:
    """Format a number the way the statements do: 1.234,5678."""
    whole, _, fraction = f"{number:,.{decimals}f}".partition(".")
    return f"{whole.replace(',', '.')},{fraction}"


def statement_text(period: tuple[str, str], **prices: float) -> list[str]:
    """Return the first-page lines of a made-up statement.

    ``prices`` takes brugsvand, varmt_vand and varme. The period line comes last
    like in the real PDFs.
    """
    lines = ["Statement of a made-up flat", "I alt kr. 1.234.567,89"]
    if (price := prices.get("brugsvand")) is not None:
        lines.append(
            f"111.111,11 22.222,22 Brugsvand kubikmeter {danish(price)} 10,0000 "
            f"{danish(price * 10, 2)}"
        )
    if (price := prices.get("varme")) is not None:
        lines.append(
            f"333.333,33 444.444,44 Varme KWh {danish(price)} 1.000,0000 "
            f"{danish(price * 1000, 2)} #"
        )
    lines.append("555.555,55 66.666,66 Varme kvadratmeter 12,3456789 100,0000 1.234,57")
    if (price := prices.get("varmt_vand")) is not None:
        lines.append(
            f"777.777,77 8.888,88 Varmt vand kubikmeter {danish(price)} 10,0000 "
            f"{danish(price * 10, 2)}"
        )
    lines.append("99.999,99 9.999,99 Varmt vand haneandele 11,1111111 2,0000 22,22")
    lines.append(f"For perioden: {period[0]} - {period[1]}")
    return lines


def make_pdf(lines: Sequence[str]) -> bytes:
    """Build a one-page PDF whose text is the given lines, one per row."""

    def escape(line: str) -> str:
        return (
            line.encode("cp1252")
            .decode("latin-1")
            .replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )

    content = "BT\n/F1 9 Tf\n"
    for row, line in enumerate(lines):
        content += f"1 0 0 1 40 {780 - 12 * row} Tm ({escape(line)}) Tj\n"
    content += "ET\n"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        f"<< /Length {len(content)} >>\nstream\n{content}endstream",
        (
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            "/Encoding /WinAnsiEncoding >>"
        ),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(pdf)


@dataclass
class StatementDoc:
    """A statement ista lists and serves as a PDF."""

    document_id: str
    atype: str
    period: tuple[str, str]
    prices: dict[str, float] = field(default_factory=dict)
    account_id: str = STATEMENT_ACCOUNT_ID
    lines: list[str] | None = None
    response: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        """Return the URL the integration downloads the document from."""
        return STATEMENT_URL.format(
            cons_id=CONS_ID,
            document_id=self.document_id,
            works_id=WORKS_ID,
            inst_id=INST_ID,
        )

    def row(self) -> dict[str, Any]:
        """Return the entry in ista's statement list."""
        start, end = (value.replace("-", ".") for value in self.period)
        return {
            "works_id": WORKS_ID,
            "inst_id": INST_ID,
            "cons_id": CONS_ID,
            "document_id": self.document_id,
            "doctype_name": "Statement",
            "doc_date": "2026-03-23 12:00:00",
            "account_id": self.account_id,
            "period": f"{start} - {end}",
            "atype_name": self.atype,
            "official": "Y",
            "d3_id": None,
            "shipjobcontentid": "100000001",
        }


def water_statement(
    document_id: str, period: tuple[str, str], brugsvand: float, **kwargs: Any
) -> StatementDoc:
    """Return a Brugsvandsregnskab with a cold water price."""
    return StatementDoc(
        document_id, "Brugsvandsregnskab", period, {"brugsvand": brugsvand}, **kwargs
    )


def heat_statement(
    document_id: str,
    period: tuple[str, str],
    varme: float,
    varmt_vand: float,
    **kwargs: Any,
) -> StatementDoc:
    """Return a Varmeregnskab with heat and hot water heating prices."""
    return StatementDoc(
        document_id,
        "Varmeregnskab",
        period,
        {"varme": varme, "varmt_vand": varmt_vand},
        **kwargs,
    )


def mock_statements(
    aioclient_mock: AiohttpClientMocker,
    docs: Sequence[StatementDoc],
    list_response: dict[str, Any] | None = None,
) -> None:
    """Register the statement list and the downloads of the documents."""
    aioclient_mock.get(
        STATEMENTS_URL,
        **(list_response or {"json": {"pdfContentList": [doc.row() for doc in docs]}}),
    )
    for doc in docs:
        response = doc.response or {
            "content": make_pdf(doc.lines or statement_text(doc.period, **doc.prices))
        }
        aioclient_mock.get(doc.url, **response)


def mock_ista(
    aioclient_mock: AiohttpClientMocker,
    *,
    login: Any = None,
    meters: Any = None,
    meters_side_effect: Any = None,
    usage: dict[str, Any] | None = None,
    statements: Sequence[StatementDoc] = (),
    statements_response: dict[str, Any] | None = None,
) -> None:
    """Register successful responses for every endpoint.

    ``usage`` overrides the daily series by name, e.g. ``{"WaterCold": [...]}``.
    ``statements`` are the settlement statements ista lists, none by default;
    ``statements_response`` replaces the answer to the list request.
    """
    aioclient_mock.post(AUTH_URL, json=login or load_json("token.json"))
    if meters_side_effect:
        aioclient_mock.get(METERS_URL, side_effect=meters_side_effect)
    else:
        aioclient_mock.get(METERS_URL, json=meters or load_json("meters.json"))
    for series in USAGE_SERIES.values():
        payload = (usage or {}).get(series) or load_json(f"usage_{series}.json")
        aioclient_mock.get(usage_url(series), json=payload)
    mock_statements(aioclient_mock, statements, statements_response)


def sequence(
    *responses: dict[str, Any],
) -> Callable[[str, Any, Any], Awaitable[AiohttpClientMockResponse]]:
    """Answer successive requests with the given responses, then repeat the last."""
    queue = list(responses)

    async def side_effect(method: str, url: Any, data: Any):
        kwargs = dict(queue.pop(0) if len(queue) > 1 else queue[0])
        if "content" in kwargs:
            kwargs["response"] = kwargs.pop("content")
        return AiohttpClientMockResponse(method=method, url=url, **kwargs)

    return side_effect


def requests_to(
    aioclient_mock: AiohttpClientMocker, url: str
) -> list[tuple[str, Any, Any, Any]]:
    """Return the recorded requests whose URL starts with ``url``."""
    return [call for call in aioclient_mock.mock_calls if str(call[1]).startswith(url)]


def create_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Return a config entry for the fixture account."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONS_ID,
        title="Test User",
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options=options,
    )


async def setup_integration(
    hass: HomeAssistant, entry: MockConfigEntry | None = None
) -> MockConfigEntry:
    """Add the entry and set it up in the Copenhagen time zone."""
    entry = entry or create_entry()
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    hass.config.currency = "DKK"
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def get_statistics(
    hass: HomeAssistant, statistic_id: str
) -> list[dict[str, float]]:
    """Return the stored hourly rows (start, state, sum) of a statistic."""
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        {"start": row["start"], "state": row["state"], "sum": row["sum"]}
        for row in stats.get(statistic_id, [])
    ]


async def get_statistic_metadata(
    hass: HomeAssistant, statistic_ids: set[str]
) -> dict[str, Any]:
    """Return the stored metadata of the given statistics."""
    await async_wait_recording_done(hass)
    metadata = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=statistic_ids)
    )
    return {statistic_id: meta for statistic_id, (_, meta) in metadata.items()}
