"""DataUpdateCoordinator for ista Denmark."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IstaDkApi, IstaDkAuthError, IstaDkError, Meter, UsageDay
from .const import DOMAIN, UPDATE_INTERVAL, MeterKind
from .prices import Estimates, PriceTable
from .statements import StatementPrices
from .statistics import async_import_cost_statistics, async_import_statistics

_LOGGER = logging.getLogger(__name__)

type IstaDkConfigEntry = ConfigEntry[IstaDkCoordinator]


@dataclass(slots=True)
class IstaDkData:
    """Latest meter readings, daily series and unit prices."""

    meters: dict[MeterKind, Meter]
    usage: dict[MeterKind, list[UsageDay]]
    prices: PriceTable


class IstaDkCoordinator(DataUpdateCoordinator[IstaDkData]):
    """Polls ista and keeps the daily statistics up to date."""

    config_entry: IstaDkConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: IstaDkConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        assert config_entry.unique_id is not None
        self.cons_id: str = config_entry.unique_id
        self.api = IstaDkApi(
            async_get_clientsession(hass),
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
        )
        self.statements = StatementPrices(hass, config_entry.entry_id)

    async def _async_update_data(self) -> IstaDkData:
        usage: dict[MeterKind, list[UsageDay]] = {}
        try:
            meters = await self.api.async_get_meters()
            for kind in meters:
                usage[kind] = await self.api.async_get_usage(kind)
            prices = await self.statements.async_update(
                self.api, Estimates.from_options(self.config_entry.options)
            )
        except IstaDkAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IstaDkError as err:
            raise UpdateFailed(str(err)) from err

        for kind, days in usage.items():
            total = await async_import_statistics(self.hass, self.cons_id, kind, days)
            if kind is MeterKind.HEAT_UNITS:
                meters[kind] = _weighted_units(meters[kind], days, total)
            await async_import_cost_statistics(
                self.hass, self.cons_id, kind, days, prices
            )
        return IstaDkData(meters=meters, usage=usage, prices=prices)


def _weighted_units(meter: Meter, days: list[UsageDay], total: float | None) -> Meter:
    """Report heat units as the total of the daily series instead of the registers.

    The registers of heat cost allocators are raw counts, while the daily series
    and the statements weigh every radiator with its own factor. The total is the
    running sum of the imported statistic, so it never drops when ista leaves
    old days out of the series.
    """
    with_value = [day.day for day in days if day.value is not None]
    return replace(
        meter,
        reading=total,
        reading_date=max(with_value) if with_value else None,
    )
