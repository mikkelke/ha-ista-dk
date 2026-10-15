"""The ista Denmark integration."""

from __future__ import annotations

from homeassistant.components.recorder import get_instance
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import IstaDkConfigEntry, IstaDkCoordinator
from .statements import async_remove_statement_cache
from .statistics import statistic_ids

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: IstaDkConfigEntry) -> bool:
    """Set up ista Denmark from a config entry."""
    coordinator = IstaDkCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IstaDkConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: IstaDkConfigEntry) -> None:
    """Delete the statistics and cached statement prices of a removed entry."""
    if cons_id := entry.unique_id:
        get_instance(hass).async_clear_statistics(statistic_ids(cons_id))
    await async_remove_statement_cache(hass, entry.entry_id)
