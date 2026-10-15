"""Tests for reading statements and looking up unit prices."""

from datetime import date

import pytest

from custom_components.ista_dk.const import MeterKind, PriceComponent
from custom_components.ista_dk.prices import (
    CurrentPrice,
    Estimates,
    PriceEntry,
    PriceTable,
    is_placeholder,
    parse_statement,
)
from custom_components.ista_dk.statements import extract_text

from .common import make_pdf, statement_text

BRUGSVAND_LINE = "111.111,11 22.222,22 Brugsvand kubikmeter 12,3456789 12,0000 148,14"
VARME_LINE = "333.333,33 444.444,44 Varme KWh 0,5432100 3.456,0000 1.877,34"
VARMT_VAND_LINE = "777.777,77 8.888,88 Varmt vand kubikmeter 21,0987654 7,5000 158,24"
PERIOD_LINE = "For perioden: 01-12-2024 - 30-11-2025"
FIXED_COST_LINES = [
    "555.555,55 66.666,66 Varme kvadratmeter 12,3456789 100,0000 1.234,57",
    "99.999,99 9.999,99 Varmt vand haneandele 11,1111111 2,0000 22,22",
    "99.999,99 99.999,99 Energistyring 9,9999999 999,9999 999,99",
    "Fjernvarme 1.234,56 MWh 1.234.567,89",
    "I alt kr. 1.234.567,89",
]

BRUGSVAND = PriceComponent.BRUGSVAND
VARMT_VAND = PriceComponent.VARMT_VAND
VARME = PriceComponent.VARME
P1 = (date(2023, 12, 1), date(2024, 11, 30))
P2 = (date(2024, 12, 1), date(2025, 11, 30))
P3 = (date(2025, 12, 1), date(2026, 11, 30))


def _entry(component: PriceComponent, period: tuple[date, date], price: float):
    return PriceEntry(component, period[0], period[1], price)


def _text(*lines: str) -> str:
    return "\n".join(lines)


def test_water_statement() -> None:
    """The Brugsvand price and the period are read from a water statement."""
    entries = parse_statement(
        _text(
            "Brugsvandsregnskab", BRUGSVAND_LINE, "Efterbetaling kr. 1,00", PERIOD_LINE
        )
    )

    assert entries == [_entry(BRUGSVAND, P2, 12.3456789)]


def test_heat_statement_ignores_fixed_cost_lines() -> None:
    """Heat and hot water heating are read, the fixed charges are not."""
    entries = parse_statement(
        _text(
            VARME_LINE,
            FIXED_COST_LINES[0],
            VARMT_VAND_LINE,
            *FIXED_COST_LINES[1:],
            PERIOD_LINE,
        )
    )

    assert entries == [_entry(VARME, P2, 0.54321), _entry(VARMT_VAND, P2, 21.0987654)]


def test_trailing_marker_is_ignored() -> None:
    """A trailing hash after the amounts does not matter."""
    entries = parse_statement(_text(VARME_LINE + " #", PERIOD_LINE))

    assert entries == [_entry(VARME, P2, 0.54321)]


def test_period_of_the_first_statement_can_start_mid_month() -> None:
    """The period is taken as printed, it is not forced to a financial year."""
    entries = parse_statement(
        _text(BRUGSVAND_LINE, "For perioden: 15-03-2020 - 30-11-2021")
    )

    assert entries == [
        PriceEntry(BRUGSVAND, date(2020, 3, 15), date(2021, 11, 30), 12.3456789)
    ]


@pytest.mark.parametrize("separator", ["-", "–", "—"])
def test_period_separator_variants(separator: str) -> None:
    """Hyphen, en dash and em dash all separate the two dates."""
    entries = parse_statement(
        _text(BRUGSVAND_LINE, f"For perioden: 01-12-2024 {separator} 30-11-2025")
    )

    assert [(e.start, e.end) for e in entries] == [P2]


def test_period_line_can_come_first_or_last() -> None:
    """The period line position in the extracted text does not matter."""
    first = parse_statement(_text(PERIOD_LINE, BRUGSVAND_LINE))
    last = parse_statement(_text(BRUGSVAND_LINE, PERIOD_LINE))

    assert first == last == [_entry(BRUGSVAND, P2, 12.3456789)]


def test_thousands_separator_in_the_price() -> None:
    """Danish number format: dot for thousands, comma for decimals."""
    line = "1,00 2,00 Varme KWh 1.234,5678900 1.000,0000 1.234.567,89"

    assert parse_statement(_text(line, PERIOD_LINE)) == [_entry(VARME, P2, 1234.56789)]


@pytest.mark.parametrize(
    "line",
    [
        "1,00 2,00 BRUGSVAND KUBIKMETER 12,3456789 12,0000 148,14",
        "1,00 2,00 brugsvand   kubikmeter 12,3456789 12,0000 148,14",
        "Brugsvand kubikmeter 12,3456789",
    ],
)
def test_label_matching_is_case_and_spacing_tolerant(line: str) -> None:
    """Letter case, repeated spaces and missing leading numbers are fine."""
    assert parse_statement(_text(line, PERIOD_LINE)) == [
        _entry(BRUGSVAND, P2, 12.3456789)
    ]


def test_missing_lines() -> None:
    """Only the components that are on the statement are returned."""
    assert parse_statement(_text(VARME_LINE, PERIOD_LINE)) == [
        _entry(VARME, P2, 0.54321)
    ]
    assert parse_statement(_text(*FIXED_COST_LINES, PERIOD_LINE)) == []
    assert parse_statement(_text(BRUGSVAND_LINE)) == []
    assert parse_statement("") == []


@pytest.mark.parametrize(
    "period",
    [
        "For perioden: 31-02-2025 - 30-11-2025",
        "For perioden: 30-11-2025 - 01-12-2024",
        "For perioden: soon",
    ],
)
def test_unusable_period_gives_nothing(period: str) -> None:
    """Without a valid period the prices cannot be placed in time."""
    assert parse_statement(_text(BRUGSVAND_LINE, period)) == []


def test_first_line_of_a_component_wins() -> None:
    """A repeated line for the same component does not override the first."""
    other = "1,00 2,00 Varme KWh 9,9999999 1,0000 9,99"

    assert parse_statement(_text(VARME_LINE, other, PERIOD_LINE)) == [
        _entry(VARME, P2, 0.54321)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Denne service er ikke tilgængelig",
        "Informationsside\n  DENNE   SERVICE ER IKKE\nTILGÆNGELIG  ",
    ],
)
def test_placeholder_is_recognised(text: str) -> None:
    """ista's stand-in document is detected and has no prices."""
    assert is_placeholder(text)
    assert parse_statement(text) == []


def test_statement_is_not_a_placeholder() -> None:
    """A real statement is not mistaken for the stand-in."""
    assert not is_placeholder(_text(BRUGSVAND_LINE, PERIOD_LINE))


def _table(*entries: PriceEntry, estimates: Estimates | None = None) -> PriceTable:
    return PriceTable(entries, estimates)


def test_price_inside_before_and_after_the_periods() -> None:
    """A day uses its own period, the oldest before it and the newest after it."""
    table = _table(
        _entry(BRUGSVAND, P1, 40.0),
        _entry(BRUGSVAND, P2, 44.0),
    )

    assert table.price_on(MeterKind.COLD_WATER, date(2024, 6, 1)) == 40.0
    assert table.price_on(MeterKind.COLD_WATER, date(2025, 6, 1)) == 44.0
    assert table.price_on(MeterKind.COLD_WATER, date(2024, 11, 30)) == 40.0
    assert table.price_on(MeterKind.COLD_WATER, date(2024, 12, 1)) == 44.0
    assert table.price_on(MeterKind.COLD_WATER, date(2020, 1, 1)) == 40.0
    assert table.price_on(MeterKind.COLD_WATER, date(2030, 1, 1)) == 44.0


def test_day_between_two_periods_uses_the_earlier_price() -> None:
    """The last known price carries over a gap between statements."""
    table = _table(_entry(BRUGSVAND, P1, 40.0), _entry(BRUGSVAND, P3, 48.0))

    assert table.price_on(MeterKind.COLD_WATER, date(2025, 6, 1)) == 40.0


def test_hot_water_adds_both_components_looked_up_separately() -> None:
    """Hot water is Brugsvand plus Varmt vand, each from its own period."""
    table = _table(
        _entry(BRUGSVAND, (date(2024, 1, 1), date(2024, 12, 31)), 10.0),
        _entry(BRUGSVAND, (date(2025, 1, 1), date(2025, 12, 31)), 20.0),
        _entry(VARMT_VAND, P1, 1.0),
        _entry(VARMT_VAND, P2, 2.0),
    )

    assert table.price_on(MeterKind.HOT_WATER, date(2024, 12, 15)) == 12.0
    assert table.price_on(MeterKind.HOT_WATER, date(2024, 6, 15)) == 11.0
    assert table.price_on(MeterKind.HOT_WATER, date(2025, 12, 15)) == 22.0


def test_kind_without_all_its_components_has_no_price() -> None:
    """A missing component means the price is unknown."""
    table = _table(_entry(BRUGSVAND, P1, 40.0))

    assert table.price_on(MeterKind.COLD_WATER, date(2024, 6, 1)) == 40.0
    assert table.price_on(MeterKind.HOT_WATER, date(2024, 6, 1)) is None
    assert table.price_on(MeterKind.HEAT, date(2024, 6, 1)) is None
    assert table.current(MeterKind.HOT_WATER) is None
    assert _table().newest_end is None
    assert _table().current(MeterKind.COLD_WATER) is None


def test_later_entry_replaces_the_same_period() -> None:
    """The same component and period listed twice keeps the last entry."""
    table = _table(_entry(BRUGSVAND, P1, 40.0), _entry(BRUGSVAND, P1, 41.0))

    assert table.price_on(MeterKind.COLD_WATER, date(2024, 6, 1)) == 41.0


def test_current_settled_price_and_period() -> None:
    """The newest settled period gives the current price."""
    table = _table(
        _entry(BRUGSVAND, P1, 40.0),
        _entry(BRUGSVAND, P2, 44.0),
        _entry(VARMT_VAND, P1, 1.0),
        _entry(VARMT_VAND, P2, 2.0),
        _entry(VARME, P2, 0.9),
    )

    assert table.newest_end == date(2025, 11, 30)
    assert table.current(MeterKind.COLD_WATER) == CurrentPrice(
        44.0, "settled", P2[0], P2[1]
    )
    assert table.current(MeterKind.HOT_WATER) == CurrentPrice(
        46.0, "settled", P2[0], P2[1]
    )
    assert table.current(MeterKind.HEAT) == CurrentPrice(0.9, "settled", P2[0], P2[1])


def test_current_hot_water_period_spans_both_statements() -> None:
    """When the two statements differ the period covers both."""
    table = _table(_entry(BRUGSVAND, P2, 44.0), _entry(VARMT_VAND, P1, 1.0))

    assert table.current(MeterKind.HOT_WATER) == CurrentPrice(
        45.0, "settled", P1[0], P2[1]
    )


def _estimated(**prices: float) -> Estimates:
    return Estimates(
        {PriceComponent(name): price for name, price in prices.items()},
        P2[1],
    )


SETTLED = (
    _entry(BRUGSVAND, P2, 40.0),
    _entry(VARMT_VAND, P2, 30.0),
    _entry(VARME, P2, 0.9),
)


def test_estimate_applies_after_the_settled_period_only() -> None:
    """Days up to estimate_after keep the settled price."""
    table = _table(*SETTLED, estimates=_estimated(brugsvand=50.0, varme=1.1))

    assert table.price_on(MeterKind.COLD_WATER, date(2025, 11, 30)) == 40.0
    assert table.price_on(MeterKind.COLD_WATER, date(2025, 12, 1)) == 50.0
    assert table.price_on(MeterKind.HEAT, date(2025, 11, 30)) == 0.9
    assert table.price_on(MeterKind.HEAT, date(2026, 6, 1)) == 1.1
    assert not table.estimates_expired


def test_partial_estimates_leave_other_components_settled() -> None:
    """A component without an estimate uses the newest settled price."""
    table = _table(*SETTLED, estimates=_estimated(brugsvand=50.0))

    assert table.price_on(MeterKind.COLD_WATER, date(2026, 6, 1)) == 50.0
    assert table.price_on(MeterKind.HOT_WATER, date(2026, 6, 1)) == 80.0
    assert table.price_on(MeterKind.HOT_WATER, date(2025, 6, 1)) == 70.0
    assert table.price_on(MeterKind.HEAT, date(2026, 6, 1)) == 0.9


def test_estimate_is_replaced_by_a_newer_statement() -> None:
    """A statement ending after estimate_after makes the estimates expire."""
    newer = (
        _entry(BRUGSVAND, P3, 45.0),
        _entry(VARMT_VAND, P3, 31.0),
        _entry(VARME, P3, 1.0),
    )
    table = _table(*SETTLED, *newer, estimates=_estimated(brugsvand=50.0, varme=1.1))

    assert table.estimates_expired
    assert table.price_on(MeterKind.COLD_WATER, date(2026, 6, 1)) == 45.0
    assert table.price_on(MeterKind.COLD_WATER, date(2027, 6, 1)) == 45.0
    assert table.price_on(MeterKind.HEAT, date(2027, 6, 1)) == 1.0
    assert table.current(MeterKind.COLD_WATER) == CurrentPrice(
        45.0, "settled", P3[0], P3[1]
    )
    assert table.as_dict()["estimates"] == {}


def test_a_statement_for_the_same_period_does_not_expire_estimates() -> None:
    """Only a statement ending after estimate_after counts as newer."""
    table = _table(
        *SETTLED,
        _entry(VARME, (date(2025, 1, 1), P2[1]), 0.8),
        estimates=_estimated(varme=1.1),
    )

    assert not table.estimates_expired
    assert table.price_on(MeterKind.HEAT, date(2026, 6, 1)) == 1.1


def test_estimates_without_a_boundary_cover_all_days_until_a_statement_arrives() -> (
    None
):
    """Without settled statements the estimates apply to every day."""
    estimates = Estimates({BRUGSVAND: 50.0}, None)

    table = _table(estimates=estimates)
    assert table.price_on(MeterKind.COLD_WATER, date(2020, 1, 1)) == 50.0
    assert table.current(MeterKind.COLD_WATER) == CurrentPrice(
        50.0, "estimate", None, None
    )

    expired = _table(_entry(BRUGSVAND, P1, 40.0), estimates=estimates)
    assert expired.estimates_expired
    assert expired.price_on(MeterKind.COLD_WATER, date(2030, 1, 1)) == 40.0


def test_current_price_reports_the_estimate() -> None:
    """The open period starts the day after estimate_after and has no end."""
    table = _table(*SETTLED, estimates=_estimated(brugsvand=50.0, varme=1.1))

    assert table.current(MeterKind.COLD_WATER) == CurrentPrice(
        50.0, "estimate", date(2025, 12, 1), None
    )
    assert table.current(MeterKind.HOT_WATER) == CurrentPrice(
        80.0, "estimate", date(2025, 12, 1), None
    )
    assert table.current(MeterKind.HEAT) == CurrentPrice(
        1.1, "estimate", date(2025, 12, 1), None
    )


def test_current_price_is_settled_when_no_component_is_estimated() -> None:
    """Estimates for other components do not change the source."""
    table = _table(*SETTLED, estimates=_estimated(varme=1.1))

    assert table.current(MeterKind.COLD_WATER).source == "settled"
    assert table.current(MeterKind.HOT_WATER).source == "settled"
    assert table.current(MeterKind.HEAT).source == "estimate"


def test_estimate_for_a_component_without_statements() -> None:
    """Days up to estimate_after stay unpriced if nothing settled covers them."""
    table = _table(_entry(BRUGSVAND, P2, 40.0), estimates=_estimated(varmt_vand=30.0))

    assert table.price_on(MeterKind.HOT_WATER, date(2025, 6, 1)) is None
    assert table.price_on(MeterKind.HOT_WATER, date(2026, 6, 1)) == 70.0
    assert table.current(MeterKind.HOT_WATER) == CurrentPrice(
        70.0, "estimate", date(2025, 12, 1), None
    )


def test_estimates_from_options() -> None:
    """The options hold the estimates and the date they apply after."""
    estimates = Estimates.from_options(
        {
            "estimate_brugsvand": 50.0,
            "estimate_varme": 0.0,
            "estimate_after": "2025-11-30",
            "unrelated": 1,
        }
    )

    assert estimates == Estimates({BRUGSVAND: 50.0, VARME: 0.0}, date(2025, 11, 30))
    assert Estimates.from_options({}) == Estimates({}, None)
    assert Estimates.from_options({"estimate_varmt_vand": 3}).prices == {
        VARMT_VAND: 3.0
    }


def test_as_dict_lists_the_table() -> None:
    """Diagnostics get the settled periods and the estimates."""
    table = _table(*SETTLED[:1], estimates=_estimated(varme=1.1))

    assert table.as_dict() == {
        "settled": {
            "brugsvand": [{"start": "2024-12-01", "end": "2025-11-30", "price": 40.0}]
        },
        "estimates": {"varme": 1.1},
        "estimate_after": "2025-11-30",
        "estimates_expired": False,
    }


def test_prices_are_read_from_the_first_page_of_a_pdf() -> None:
    """The text layer of a PDF goes through extraction and parsing."""
    lines = statement_text(
        ("15-03-2020", "30-11-2021"), brugsvand=1234.5, varme=0.5, varmt_vand=21.5
    )

    text = extract_text(make_pdf(lines))

    assert text.splitlines() == lines
    assert parse_statement(text) == [
        PriceEntry(BRUGSVAND, date(2020, 3, 15), date(2021, 11, 30), 1234.5),
        PriceEntry(VARME, date(2020, 3, 15), date(2021, 11, 30), 0.5),
        PriceEntry(VARMT_VAND, date(2020, 3, 15), date(2021, 11, 30), 21.5),
    ]


def test_placeholder_document_is_recognised_in_a_pdf() -> None:
    """ista's stand-in PDF is detected from its text."""
    text = extract_text(
        make_pdf(["Informationsside", "Denne service er ikke tilgængelig"])
    )

    assert is_placeholder(text)
