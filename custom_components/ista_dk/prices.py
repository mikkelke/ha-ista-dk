"""Unit prices from ista settlement statements and the lookup of the price per day."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import re
from typing import Any

from .const import (
    CONF_ESTIMATE_AFTER,
    ESTIMATE_OPTIONS,
    SOURCE_ESTIMATE,
    SOURCE_SETTLED,
    MeterKind,
    PriceComponent,
)

KIND_COMPONENTS: dict[MeterKind, tuple[PriceComponent, ...]] = {
    MeterKind.COLD_WATER: (PriceComponent.BRUGSVAND,),
    MeterKind.HOT_WATER: (PriceComponent.BRUGSVAND, PriceComponent.VARMT_VAND),
    MeterKind.HEAT: (PriceComponent.VARME,),
}

_AMOUNT = r"(?:\d{1,3}(?:\.\d{3})+|\d+),\d+"
_PERIOD = re.compile(
    r"For\s+perioden:\s*(\d{2}-\d{2}-\d{4})\s*[-–—]\s*(\d{2}-\d{2}-\d{4})",
    re.IGNORECASE,
)
_PRICE_LINE = re.compile(
    rf"\b(Brugsvand\s+kubikmeter|Varmt\s+vand\s+kubikmeter|Varme\s+KWh)\s+({_AMOUNT})",
    re.IGNORECASE,
)
_LABEL_COMPONENT = {
    "brugsvand kubikmeter": PriceComponent.BRUGSVAND,
    "varmt vand kubikmeter": PriceComponent.VARMT_VAND,
    "varme kwh": PriceComponent.VARME,
}
_PLACEHOLDER = "denne service er ikke tilgængelig"


@dataclass(frozen=True, slots=True)
class PriceEntry:
    """The unit price of one component for one statement period."""

    component: PriceComponent
    start: date
    end: date
    price: float


@dataclass(frozen=True, slots=True)
class CurrentPrice:
    """The unit price that applies now and where it comes from."""

    price: float
    source: str
    period_start: date | None
    period_end: date | None


@dataclass(frozen=True, slots=True)
class Estimates:
    """Estimated prices for days after the newest settled statement."""

    prices: Mapping[PriceComponent, float] = field(default_factory=dict)
    after: date | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Estimates:
        """Read the estimates stored in a config entry's options."""
        prices = {
            component: float(value)
            for component, key in ESTIMATE_OPTIONS.items()
            if (value := options.get(key)) is not None
        }
        after = options.get(CONF_ESTIMATE_AFTER)
        return cls(prices, date.fromisoformat(after) if after else None)


def _parse_number(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", "."))


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%d-%m-%Y").date()


def is_placeholder(text: str) -> bool:
    """Return whether the text is ista's stand-in for a missing document."""
    return _PLACEHOLDER in " ".join(text.split()).casefold()


def parse_statement(text: str) -> list[PriceEntry]:
    """Read the unit prices of one statement from the text of its first page.

    A statement covers one "For perioden" span and the price lines can come
    before or after it. When a component has several lines the first one counts.
    """
    if (period := _PERIOD.search(text)) is None:
        return []
    try:
        start, end = _parse_date(period[1]), _parse_date(period[2])
    except ValueError:
        return []
    if start > end:
        return []

    prices: dict[PriceComponent, float] = {}
    for line in text.splitlines():
        if match := _PRICE_LINE.search(line):
            component = _LABEL_COMPONENT[" ".join(match[1].lower().split())]
            prices.setdefault(component, _parse_number(match[2]))
    return [
        PriceEntry(component, start, end, price) for component, price in prices.items()
    ]


def _period_on(periods: list[PriceEntry], day: date) -> PriceEntry:
    containing = [entry for entry in periods if entry.start <= day <= entry.end]
    if containing:
        return containing[-1]
    earlier = [entry for entry in periods if entry.end < day]
    if earlier:
        return max(earlier, key=lambda entry: entry.end)
    return periods[0]


class PriceTable:
    """Settled prices per statement period plus optional estimates.

    Without estimates a day uses the period that contains it, the newest period
    after the last one and the oldest period before the first one. Estimates
    replace that for days after ``estimates.after`` until a statement ending
    after it has arrived.
    """

    def __init__(
        self,
        entries: Iterable[PriceEntry] = (),
        estimates: Estimates | None = None,
    ) -> None:
        unique = {(e.component, e.start, e.end): e for e in entries}
        self._periods: dict[PriceComponent, list[PriceEntry]] = {}
        for entry in sorted(unique.values(), key=lambda e: (e.start, e.end)):
            self._periods.setdefault(entry.component, []).append(entry)
        self.newest_end: date | None = max(
            (e.end for e in unique.values()), default=None
        )

        estimates = estimates or Estimates()
        self._estimate_after = estimates.after
        self.estimates_expired = (
            bool(estimates.prices)
            and self.newest_end is not None
            and (estimates.after is None or self.newest_end > estimates.after)
        )
        self._estimates = {} if self.estimates_expired else dict(estimates.prices)

    def price_on(self, kind: MeterKind, day: date) -> float | None:
        """Return the unit price of a meter kind on a day, None if unknown."""
        total = 0.0
        for component in KIND_COMPONENTS[kind]:
            if (price := self._component_price_on(component, day)) is None:
                return None
            total += price
        return total

    def current(self, kind: MeterKind) -> CurrentPrice | None:
        """Return the unit price for the open period, None if unknown."""
        prices: list[float] = []
        settled: list[PriceEntry] = []
        for component in KIND_COMPONENTS[kind]:
            if component in self._estimates:
                prices.append(self._estimates[component])
            elif periods := self._periods.get(component):
                newest = max(periods, key=lambda e: (e.end, e.start))
                prices.append(newest.price)
                settled.append(newest)
            else:
                return None

        if len(settled) < len(prices):
            after = self._estimate_after
            return CurrentPrice(
                sum(prices),
                SOURCE_ESTIMATE,
                after + timedelta(days=1) if after else None,
                None,
            )
        return CurrentPrice(
            sum(prices),
            SOURCE_SETTLED,
            min(entry.start for entry in settled),
            max(entry.end for entry in settled),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the table as plain data for diagnostics."""
        return {
            "settled": {
                str(component): [
                    {
                        "start": entry.start.isoformat(),
                        "end": entry.end.isoformat(),
                        "price": entry.price,
                    }
                    for entry in entries
                ]
                for component, entries in self._periods.items()
            },
            "estimates": {
                str(component): price for component, price in self._estimates.items()
            },
            "estimate_after": (
                self._estimate_after.isoformat() if self._estimate_after else None
            ),
            "estimates_expired": self.estimates_expired,
        }

    def _component_price_on(self, component: PriceComponent, day: date) -> float | None:
        if component in self._estimates and (
            self._estimate_after is None or day > self._estimate_after
        ):
            return self._estimates[component]
        if periods := self._periods.get(component):
            return _period_on(periods, day).price
        return None
