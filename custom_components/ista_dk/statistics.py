"""Import of the daily consumption series as external long-term statistics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
import logging
import math

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import UsageDay
from .const import DOMAIN, MeterKind
from .prices import PriceTable

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StatisticKind:
    name: str
    unit: str
    unit_class: str


STATISTIC_KINDS: dict[MeterKind, _StatisticKind] = {
    MeterKind.COLD_WATER: _StatisticKind(
        "ista Cold water", UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS
    ),
    MeterKind.HOT_WATER: _StatisticKind(
        "ista Hot water", UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS
    ),
    MeterKind.HEAT: _StatisticKind(
        "ista Heat", UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS
    ),
}


def statistic_id(cons_id: str, kind: MeterKind) -> str:
    """Return the external statistic id for a meter kind of an account."""
    return f"{DOMAIN}:{slugify(cons_id)}_{kind}"


def cost_statistic_id(cons_id: str, kind: MeterKind) -> str:
    """Return the external cost statistic id for a meter kind of an account."""
    return f"{statistic_id(cons_id, kind)}_cost"


def statistic_ids(cons_id: str) -> list[str]:
    """Return every external statistic id of an account, costs included."""
    return [
        stat_id
        for kind in MeterKind
        for stat_id in (statistic_id(cons_id, kind), cost_statistic_id(cons_id, kind))
    ]


def _day_start(day: date) -> datetime:
    return dt_util.as_utc(dt_util.start_of_local_day(day))


def build_statistics(
    days: Iterable[UsageDay],
    *,
    after: datetime | None = None,
    running_sum: float = 0.0,
) -> list[StatisticData]:
    """Build one statistics row per day with a value.

    Rows start at local midnight of their day. Days whose row would start at
    or before ``after`` are already stored and skipped; ``running_sum`` is the
    stored cumulative sum at that point.
    """
    statistics: list[StatisticData] = []
    for usage_day in sorted(days, key=lambda usage_day: usage_day.day):
        if usage_day.value is None:
            continue
        start = _day_start(usage_day.day)
        if after is not None and start <= after:
            continue
        running_sum += usage_day.value
        statistics.append(
            {"start": start, "state": usage_day.value, "sum": running_sum}
        )
    return statistics


async def async_import_statistics(
    hass: HomeAssistant,
    cons_id: str,
    kind: MeterKind,
    days: Iterable[UsageDay],
) -> None:
    """Insert the days newer than the last stored statistic."""
    stat_id = statistic_id(cons_id, kind)
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, False, {"sum"}
    )

    after = None
    running_sum = 0.0
    if last_stats:
        last = last_stats[stat_id][0]
        after = dt_util.utc_from_timestamp(last["start"])
        running_sum = last.get("sum") or 0.0

    statistics = build_statistics(days, after=after, running_sum=running_sum)
    if not statistics:
        return

    statistic_kind = STATISTIC_KINDS[kind]
    metadata: StatisticMetaData = {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": statistic_kind.name,
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_class": statistic_kind.unit_class,
        "unit_of_measurement": statistic_kind.unit,
    }
    _LOGGER.debug("Importing %d statistics for %s", len(statistics), stat_id)
    async_add_external_statistics(hass, metadata, statistics)


def build_cost_statistics(
    days: Iterable[UsageDay], prices: PriceTable, kind: MeterKind
) -> list[StatisticData]:
    """Price every day with a value; days without a unit price are skipped."""
    statistics: list[StatisticData] = []
    total = 0.0
    for usage_day in sorted(days, key=lambda usage_day: usage_day.day):
        if usage_day.value is None:
            continue
        if (price := prices.price_on(kind, usage_day.day)) is None:
            continue
        cost = usage_day.value * price
        total += cost
        statistics.append(
            {"start": _day_start(usage_day.day), "state": cost, "sum": total}
        )
    return statistics


def _read_stored(
    hass: HomeAssistant, stat_id: str
) -> dict[float, tuple[float | None, float | None]]:
    stored = statistics_during_period(
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return {
        row["start"]: (row.get("state"), row.get("sum"))
        for row in stored.get(stat_id, [])
    }


def _is_stored(
    stored: dict[float, tuple[float | None, float | None]], row: StatisticData
) -> bool:
    values = stored.get(row["start"].timestamp())
    return values is not None and all(
        value is not None and math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9)
        for value, expected in zip(values, (row["state"], row["sum"]), strict=True)
    )


async def async_import_cost_statistics(
    hass: HomeAssistant,
    cons_id: str,
    kind: MeterKind,
    days: Iterable[UsageDay],
    prices: PriceTable,
) -> None:
    """Rebuild the cost series and write the rows that differ from the stored ones."""
    statistics = build_cost_statistics(days, prices, kind)
    if not statistics:
        return

    stat_id = cost_statistic_id(cons_id, kind)
    stored = await get_instance(hass).async_add_executor_job(
        _read_stored, hass, stat_id
    )
    changed = [row for row in statistics if not _is_stored(stored, row)]
    if not changed:
        return

    metadata: StatisticMetaData = {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"{STATISTIC_KINDS[kind].name} cost",
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_class": None,
        "unit_of_measurement": hass.config.currency,
    }
    _LOGGER.debug("Importing %d cost statistics for %s", len(changed), stat_id)
    async_add_external_statistics(hass, metadata, changed)
