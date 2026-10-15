"""Tests for the ista Denmark API client."""

from datetime import date, timedelta
from typing import Any

import aiohttp
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ista_dk.api import (
    IstaDkApi,
    IstaDkApiError,
    IstaDkAuthError,
    LoginInfo,
    Meter,
    Statement,
    UsageDay,
)
from custom_components.ista_dk.const import (
    AUTH_URL,
    METERS_URL,
    STATEMENT_ACCOUNT_ID,
    STATEMENTS_URL,
    MeterKind,
)

from .common import (
    CONS_ID,
    INST_ID,
    PASSWORD,
    USERNAME,
    WORKS_ID,
    StatementDoc,
    heat_statement,
    load_json,
    mock_ista,
    requests_to,
    sequence,
    usage_url,
    water_statement,
)


@pytest.fixture
async def api(hass: HomeAssistant, aioclient_mock) -> IstaDkApi:
    """Return a client bound to the mocked HTTP session."""
    return IstaDkApi(async_get_clientsession(hass), USERNAME, PASSWORD)


async def test_login_returns_account_details(api, aioclient_mock) -> None:
    """A login posts the password grant and returns the account details."""
    mock_ista(aioclient_mock)

    assert await api.async_login() == LoginInfo(cons_id=CONS_ID, first_name="Test User")

    ((method, url, data, _),) = aioclient_mock.mock_calls
    assert (method.lower(), str(url)) == ("post", AUTH_URL)
    assert data == {
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }


@pytest.mark.parametrize("status", [400, 401])
async def test_rejected_credentials_raise_auth_error(
    api, aioclient_mock, status
) -> None:
    """HTTP 400 and 401 from the token endpoint mean bad credentials."""
    aioclient_mock.post(AUTH_URL, status=status)

    with pytest.raises(IstaDkAuthError):
        await api.async_login()


@pytest.mark.parametrize(
    "login_response",
    [
        {"status": 500},
        {"exc": aiohttp.ClientError("boom")},
        {"exc": TimeoutError()},
        {"json": {}},
        {"json": ["not", "an", "object"]},
        {"json": {"access_token": "FAKE_TOKEN"}},
        {"text": "<html>maintenance</html>"},
    ],
)
async def test_login_failures_raise_api_error(
    api, aioclient_mock, login_response
) -> None:
    """Server errors, network errors and unusable answers are API errors."""
    aioclient_mock.post(AUTH_URL, **login_response)

    with pytest.raises(IstaDkApiError):
        await api.async_login()


async def test_get_meters_returns_register_per_kind(api, aioclient_mock) -> None:
    """Meters are mapped by type and read with the bearer token."""
    mock_ista(aioclient_mock)

    meters = await api.async_get_meters()

    assert meters == {
        MeterKind.COLD_WATER: Meter(MeterKind.COLD_WATER, 412.75, date(2026, 10, 7)),
        MeterKind.HOT_WATER: Meter(MeterKind.HOT_WATER, 205.125, date(2026, 10, 7)),
        MeterKind.HEAT: Meter(MeterKind.HEAT, 3000.0, date(2026, 10, 7)),
    }
    ((_, _, _, headers),) = requests_to(aioclient_mock, METERS_URL)
    assert headers == {"Authorization": "Bearer FAKE_TOKEN"}


async def test_unknown_meter_types_and_bad_readings(api, aioclient_mock) -> None:
    """Unknown meter types are ignored and unreadable values become None."""
    meters = load_json("meters.json")
    rows = meters["Meters"]["Value"]
    rows.append({**rows[0], "MeterType": "SOLAR"})
    rows[0]["Last_Meter_Reading"] = None
    rows[0]["Reading_date"] = "not a date"
    mock_ista(aioclient_mock, meters=meters)

    result = await api.async_get_meters()

    assert set(result) == set(MeterKind)
    assert result[MeterKind.HOT_WATER] == Meter(MeterKind.HOT_WATER, None, None)


@pytest.mark.parametrize(
    "payload",
    [{}, {"Meters": None}, {"Meters": {"Value": [1]}}, {"Meters": {"Value": [{}]}}, []],
)
async def test_unexpected_meters_payload_raises_api_error(
    api, aioclient_mock, payload
) -> None:
    """A payload without the expected structure is an API error."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(METERS_URL, json=payload)

    with pytest.raises(IstaDkApiError):
        await api.async_get_meters()


async def test_get_usage_uses_ista_headers_and_parses_days(api, aioclient_mock) -> None:
    """The daily series is requested with the raw token and parsed oldest first."""
    mock_ista(aioclient_mock)

    days = await api.async_get_usage(MeterKind.COLD_WATER)

    ((_, url, _, headers),) = requests_to(aioclient_mock, usage_url("WaterCold"))
    assert url.query == {"inverval": "1"}
    assert headers == {"IstaAuthentication": "FAKE_TOKEN", "IstaLanguage": "da-DK"}

    assert len(days) == 48
    assert days == sorted(days, key=lambda day: day.day)
    assert days[0] == UsageDay(date(2020, 1, 1), None)
    assert UsageDay(date(2026, 10, 7), 0.06) in days
    assert days[-1] == UsageDay(date(2026, 10, 10), None)
    assert sum(day.value is not None for day in days) == 42


@pytest.mark.parametrize(
    ("kind", "series"),
    [
        (MeterKind.COLD_WATER, "WaterCold"),
        (MeterKind.HOT_WATER, "WaterHot"),
        (MeterKind.HEAT, "Energy"),
    ],
)
async def test_usage_series_per_kind(api, aioclient_mock, kind, series) -> None:
    """Each meter kind reads its own series."""
    mock_ista(aioclient_mock)

    await api.async_get_usage(kind)

    assert len(requests_to(aioclient_mock, usage_url(series))) == 1


async def test_usage_is_sorted_oldest_first(api, aioclient_mock) -> None:
    """Rows are returned in date order whatever order ista sends them in."""
    rows = [
        {"date": "2026-10-02T00:00:00", "value": 2.0},
        {"date": "2026-10-01T00:00:00", "value": None},
        {"date": "2026-10-03T00:00:00", "value": 0.0},
    ]
    mock_ista(aioclient_mock, usage={"WaterCold": rows})

    days = await api.async_get_usage(MeterKind.COLD_WATER)

    assert days == [
        UsageDay(date(2026, 10, 1), None),
        UsageDay(date(2026, 10, 2), 2.0),
        UsageDay(date(2026, 10, 3), 0.0),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2026-10-01T00:00:00", "value": 1.0},
        [{"value": 1.0}],
        [{"date": "yesterday", "value": 1.0}],
        [{"date": "2026-10-01T00:00:00", "value": "lots"}],
        [{"date": "2026-10-01T00:00:00"}],
        ["2026-10-01"],
    ],
)
async def test_unexpected_usage_payload_raises_api_error(
    api, aioclient_mock, payload: Any
) -> None:
    """A series that is not a list of dated values is an API error."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(usage_url("WaterCold"), json=payload)

    with pytest.raises(IstaDkApiError):
        await api.async_get_usage(MeterKind.COLD_WATER)


@pytest.mark.parametrize(
    "response",
    [{"status": 503}, {"exc": aiohttp.ClientError("boom")}, {"text": "nope"}],
)
async def test_data_request_failures_raise_api_error(
    api, aioclient_mock, response
) -> None:
    """HTTP errors, network errors and non-JSON bodies are API errors."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(METERS_URL, **response)

    with pytest.raises(IstaDkApiError):
        await api.async_get_meters()


async def test_token_is_reused_until_it_expires(api, aioclient_mock, freezer) -> None:
    """One login serves requests until the token's lifetime is nearly over."""
    mock_ista(aioclient_mock)

    await api.async_get_meters()
    freezer.tick(timedelta(minutes=30))
    await api.async_get_meters()
    assert len(requests_to(aioclient_mock, AUTH_URL)) == 1

    freezer.tick(timedelta(minutes=30))
    await api.async_get_meters()
    assert len(requests_to(aioclient_mock, AUTH_URL)) == 2


async def test_token_is_refreshed_after_401(api, aioclient_mock) -> None:
    """A 401 triggers one new login and the request is retried with the new token."""
    token = load_json("token.json")
    aioclient_mock.post(
        AUTH_URL,
        side_effect=sequence(
            {"json": {**token, "access_token": "TOKEN_1"}},
            {"json": {**token, "access_token": "TOKEN_2"}},
        ),
    )
    aioclient_mock.get(
        METERS_URL,
        side_effect=sequence({"status": 401}, {"json": load_json("meters.json")}),
    )

    meters = await api.async_get_meters()

    assert meters[MeterKind.COLD_WATER].reading == 412.75
    calls = [
        (call[0].lower(), str(call[1]), call[3]) for call in aioclient_mock.mock_calls
    ]
    assert calls == [
        ("post", AUTH_URL, None),
        ("get", METERS_URL, {"Authorization": "Bearer TOKEN_1"}),
        ("post", AUTH_URL, None),
        ("get", METERS_URL, {"Authorization": "Bearer TOKEN_2"}),
    ]


async def test_usage_request_is_retried_with_new_token_after_401(
    api, aioclient_mock
) -> None:
    """The daily series endpoint gets the same refresh-and-retry treatment."""
    token = load_json("token.json")
    aioclient_mock.post(
        AUTH_URL,
        side_effect=sequence(
            {"json": {**token, "access_token": "TOKEN_1"}},
            {"json": {**token, "access_token": "TOKEN_2"}},
        ),
    )
    aioclient_mock.get(
        usage_url("WaterHot"),
        side_effect=sequence(
            {"status": 401}, {"json": load_json("usage_WaterHot.json")}
        ),
    )

    days = await api.async_get_usage(MeterKind.HOT_WATER)

    assert len(days) == 48
    headers = [call[3] for call in requests_to(aioclient_mock, usage_url("WaterHot"))]
    assert [header["IstaAuthentication"] for header in headers] == [
        "TOKEN_1",
        "TOKEN_2",
    ]


async def test_persistent_401_raises_api_error_after_one_retry(
    api, aioclient_mock
) -> None:
    """A fresh token that is still refused is an API problem, not bad credentials."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(METERS_URL, status=401)

    with pytest.raises(IstaDkApiError):
        await api.async_get_meters()

    assert len(requests_to(aioclient_mock, AUTH_URL)) == 2
    assert len(requests_to(aioclient_mock, METERS_URL)) == 2


async def test_rejected_relogin_after_401_raises_auth_error(
    api, aioclient_mock
) -> None:
    """If the refresh login is refused, the credentials are no longer valid."""
    aioclient_mock.post(
        AUTH_URL,
        side_effect=sequence({"json": load_json("token.json")}, {"status": 400}),
    )
    aioclient_mock.get(METERS_URL, status=401)

    with pytest.raises(IstaDkAuthError):
        await api.async_get_meters()


async def test_get_statements_keeps_the_consumer_statements(
    api, aioclient_mock
) -> None:
    """Forecasts and other documents are not statements."""
    docs = [
        heat_statement("100000001", ("01-12-2024", "30-11-2025"), 1.0, 2.0),
        water_statement("100000002", ("01-12-2024", "30-11-2025"), 3.0),
        StatementDoc(
            "100000003",
            "Varmeregnskab",
            ("01-12-2025", "30-11-2026"),
            account_id="FORECAST",
        ),
        StatementDoc("100000004", "Elregnskab", ("01-12-2024", "30-11-2025")),
        StatementDoc(
            "100000005",
            "Varmeregnskab",
            ("01-12-2024", "30-11-2025"),
            account_id="OTHER",
        ),
    ]
    mock_ista(aioclient_mock, statements=docs)

    statements = await api.async_get_statements()

    assert statements == [
        Statement(
            "100000001",
            WORKS_ID,
            INST_ID,
            CONS_ID,
            "Varmeregnskab 01.12.2024 - 30.11.2025",
        ),
        Statement(
            "100000002",
            WORKS_ID,
            INST_ID,
            CONS_ID,
            "Brugsvandsregnskab 01.12.2024 - 30.11.2025",
        ),
    ]
    assert docs[0].row()["account_id"] == STATEMENT_ACCOUNT_ID
    ((_, _, _, headers),) = requests_to(aioclient_mock, STATEMENTS_URL)
    assert headers == {"Authorization": "Bearer FAKE_TOKEN"}


@pytest.mark.parametrize("payload", [{"pdfContentList": []}, {"pdfContentList": None}])
async def test_get_statements_without_documents(api, aioclient_mock, payload) -> None:
    """An account without documents has no statements."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(STATEMENTS_URL, json=payload)

    assert await api.async_get_statements() == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"pdfContentList": 1},
        {"pdfContentList": ["text"]},
        {
            "pdfContentList": [
                {"account_id": STATEMENT_ACCOUNT_ID, "atype_name": "Varmeregnskab"}
            ]
        },
    ],
)
async def test_unexpected_statements_payload_raises_api_error(
    api, aioclient_mock, payload
) -> None:
    """A list that does not have the expected structure is an API error."""
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(STATEMENTS_URL, json=payload)

    with pytest.raises(IstaDkApiError):
        await api.async_get_statements()


async def test_download_statement_uses_the_documented_parameter_order(
    api, aioclient_mock
) -> None:
    """The path is cons id, document id, works id, inst id and the body is the PDF."""
    doc = water_statement("100000002", ("01-12-2024", "30-11-2025"), 3.0)
    doc.response = {"content": b"%PDF-1.4 bytes"}
    mock_ista(aioclient_mock, statements=[doc])
    statement = (await api.async_get_statements())[0]

    pdf = await api.async_download_statement(statement)

    assert pdf == b"%PDF-1.4 bytes"
    ((_, url, _, headers),) = requests_to(aioclient_mock, doc.url)
    assert str(url) == (
        f"https://prod.istaonlinebeta.dk/api/Download/{CONS_ID}/100000002/{WORKS_ID}/{INST_ID}"
    )
    assert headers == {"Authorization": "Bearer FAKE_TOKEN"}


async def test_download_is_retried_with_a_new_token_after_401(
    api, aioclient_mock
) -> None:
    """Documents get the same refresh-and-retry treatment as the data."""
    doc = water_statement("100000002", ("01-12-2024", "30-11-2025"), 3.0)
    mock_ista(aioclient_mock, statements=[doc])
    statement = (await api.async_get_statements())[0]
    aioclient_mock.clear_requests()
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(
        doc.url, side_effect=sequence({"status": 401}, {"content": b"%PDF-1.4"})
    )

    assert await api.async_download_statement(statement) == b"%PDF-1.4"
    assert len(requests_to(aioclient_mock, AUTH_URL)) == 1


async def test_download_failure_raises_api_error(api, aioclient_mock) -> None:
    """HTTP errors on a document are API errors."""
    statement = Statement("1", WORKS_ID, INST_ID, CONS_ID, "label")
    aioclient_mock.post(AUTH_URL, json=load_json("token.json"))
    aioclient_mock.get(
        f"https://prod.istaonlinebeta.dk/api/Download/{CONS_ID}/1/{WORKS_ID}/{INST_ID}",
        status=500,
    )

    with pytest.raises(IstaDkApiError):
        await api.async_download_statement(statement)
