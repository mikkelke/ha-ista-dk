"""Minimal async client for the ista Denmark consumer API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    AUTH_URL,
    METER_TYPE_TO_KIND,
    METERS_URL,
    STATEMENT_ACCOUNT_ID,
    STATEMENT_TYPES,
    STATEMENT_URL,
    STATEMENTS_URL,
    USAGE_SERIES,
    USAGE_URL,
    MeterKind,
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
DEFAULT_TOKEN_LIFETIME = timedelta(hours=1)
TOKEN_EXPIRY_MARGIN = timedelta(minutes=1)
USAGE_LANGUAGE = "da-DK"


class IstaDkError(Exception):
    """Base class for ista Denmark API errors."""


class IstaDkAuthError(IstaDkError):
    """Raised when ista rejects the consumer number or password."""


class IstaDkApiError(IstaDkError):
    """Raised when ista cannot be reached or answers unexpectedly."""


@dataclass(frozen=True, slots=True)
class LoginInfo:
    """Account details returned by a successful login."""

    cons_id: str
    first_name: str


@dataclass(frozen=True, slots=True)
class Meter:
    """The current register reading of one meter."""

    kind: MeterKind
    reading: float | None
    reading_date: date | None


@dataclass(frozen=True, slots=True)
class UsageDay:
    """Consumption on one calendar day, None while ista has no value yet."""

    day: date
    value: float | None


@dataclass(frozen=True, slots=True)
class Statement:
    """A settlement statement ista lists for the account."""

    document_id: str
    works_id: str
    inst_id: str
    cons_id: str
    label: str


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: datetime


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _usage_headers(token: str) -> dict[str, str]:
    return {"IstaAuthentication": token, "IstaLanguage": USAGE_LANGUAGE}


def _token_lifetime(raw: Any) -> timedelta:
    try:
        return timedelta(seconds=int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_LIFETIME


def _parse_reading_date(raw: Any) -> date | None:
    try:
        return datetime.strptime(raw, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    return await resp.json(content_type=None)


async def _read_bytes(resp: aiohttp.ClientResponse) -> bytes:
    return await resp.read()


def _parse_statements(payload: Any) -> list[Statement]:
    statements: list[Statement] = []
    for row in payload["pdfContentList"] or []:
        if (
            row.get("account_id") != STATEMENT_ACCOUNT_ID
            or row.get("atype_name") not in STATEMENT_TYPES
        ):
            continue
        statements.append(
            Statement(
                document_id=str(row["document_id"]),
                works_id=str(row["works_id"]),
                inst_id=str(row["inst_id"]),
                cons_id=str(row["cons_id"]),
                label=f"{row['atype_name']} {row.get('period') or ''}".strip(),
            )
        )
    return statements


def _parse_meters(payload: Any) -> dict[MeterKind, Meter]:
    meters: dict[MeterKind, Meter] = {}
    for row in payload["Meters"]["Value"]:
        kind = METER_TYPE_TO_KIND.get(row["MeterType"])
        if kind is None or kind in meters:
            continue
        reading = row.get("Last_Meter_Reading")
        meters[kind] = Meter(
            kind=kind,
            reading=None if reading is None else float(reading),
            reading_date=_parse_reading_date(row.get("Reading_date")),
        )
    return meters


def _parse_usage(payload: Any) -> list[UsageDay]:
    days: list[UsageDay] = []
    for row in payload:
        value = row["value"]
        days.append(
            UsageDay(
                # Naive local calendar day, not an instant.
                day=datetime.fromisoformat(row["date"]).date(),
                value=None if value is None else float(value),
            )
        )
    days.sort(key=lambda usage_day: usage_day.day)
    return days


class IstaDkApi:
    """Thin wrapper around the ista endpoints this integration needs."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._token: _Token | None = None

    async def async_login(self) -> LoginInfo:
        """Log in with the stored credentials and cache the access token."""
        info, self._token = await self._async_request_token()
        return info

    async def async_get_meters(self) -> dict[MeterKind, Meter]:
        """Return the current register reading per meter kind."""
        payload = await self._async_get_authorized(
            "meters", METERS_URL, _bearer_headers, _read_json
        )
        try:
            return _parse_meters(payload)
        except (KeyError, TypeError, ValueError) as err:
            raise IstaDkApiError(f"Unexpected meters response: {err!r}") from err

    async def async_get_usage(self, kind: MeterKind) -> list[UsageDay]:
        """Return the daily consumption series for a meter kind, oldest first."""
        payload = await self._async_get_authorized(
            f"the {kind} series",
            USAGE_URL.format(series=USAGE_SERIES[kind]),
            _usage_headers,
            _read_json,
            # "inverval" is the API's own spelling of the parameter.
            {"inverval": "1"},
        )
        try:
            return _parse_usage(payload)
        except (KeyError, TypeError, ValueError) as err:
            raise IstaDkApiError(f"Unexpected usage response: {err!r}") from err

    async def async_get_statements(self) -> list[Statement]:
        """Return the settlement statements ista lists for the account."""
        payload = await self._async_get_authorized(
            "the statement list", STATEMENTS_URL, _bearer_headers, _read_json
        )
        try:
            return _parse_statements(payload)
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise IstaDkApiError(f"Unexpected statements response: {err!r}") from err

    async def async_download_statement(self, statement: Statement) -> bytes:
        """Download a settlement statement as PDF bytes."""
        url = STATEMENT_URL.format(
            cons_id=quote(statement.cons_id, safe=""),
            document_id=quote(statement.document_id, safe=""),
            works_id=quote(statement.works_id, safe=""),
            inst_id=quote(statement.inst_id, safe=""),
        )
        return await self._async_get_authorized(
            "a statement", url, _bearer_headers, _read_bytes
        )

    async def _async_request_token(self) -> tuple[LoginInfo, _Token]:
        try:
            async with self._session.post(
                AUTH_URL,
                data={
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password,
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED):
                    raise IstaDkAuthError("Invalid consumer number or password")
                if resp.status != HTTPStatus.OK:
                    raise IstaDkApiError(f"ista login returned HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise IstaDkApiError(f"Error connecting to ista: {err}") from err

        if (
            not isinstance(payload, dict)
            or not payload.get("access_token")
            or not payload.get("ConsId")
        ):
            raise IstaDkApiError("Unexpected login response")

        expires_at = (
            datetime.now(UTC)
            + _token_lifetime(payload.get("expires_in"))
            - TOKEN_EXPIRY_MARGIN
        )
        return (
            LoginInfo(
                cons_id=str(payload["ConsId"]),
                first_name=str(payload.get("FirstName") or "").strip(),
            ),
            _Token(value=str(payload["access_token"]), expires_at=expires_at),
        )

    async def _async_get_token(self) -> str:
        if self._token is None or datetime.now(UTC) >= self._token.expires_at:
            _, self._token = await self._async_request_token()
        return self._token.value

    async def _async_get_authorized(
        self,
        what: str,
        url: str,
        auth_headers: Callable[[str], dict[str, str]],
        read: Callable[[aiohttp.ClientResponse], Awaitable[Any]],
        params: dict[str, str] | None = None,
    ) -> Any:
        status, payload = await self._async_get(
            url, auth_headers(await self._async_get_token()), read, params
        )
        if status == HTTPStatus.UNAUTHORIZED:
            self._token = None
            status, payload = await self._async_get(
                url, auth_headers(await self._async_get_token()), read, params
            )
        if status != HTTPStatus.OK:
            raise IstaDkApiError(f"ista returned HTTP {status} for {what}")
        return payload

    async def _async_get(
        self,
        url: str,
        headers: dict[str, str],
        read: Callable[[aiohttp.ClientResponse], Awaitable[Any]],
        params: dict[str, str] | None,
    ) -> tuple[int, Any]:
        try:
            async with self._session.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != HTTPStatus.OK:
                    return resp.status, None
                return resp.status, await read(resp)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise IstaDkApiError(f"Error connecting to ista: {err}") from err
