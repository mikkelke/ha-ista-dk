"""Diagnostics support for ista Denmark."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import UsageDay
from .coordinator import IstaDkConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD}


def _summarize_usage(days: list[UsageDay]) -> dict[str, Any]:
    with_value = [day for day in days if day.value is not None]
    return {
        "days": len(days),
        "days_with_value": len(with_value),
        "first_day": days[0].day.isoformat() if days else None,
        "last_day_with_value": (with_value[-1].day.isoformat() if with_value else None),
        "last_value": with_value[-1].value if with_value else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IstaDkConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data.data
    return {
        "entry": async_redact_data(entry.data, TO_REDACT),
        "meters": {
            str(kind): {
                "reading": meter.reading,
                "reading_date": (
                    meter.reading_date.isoformat() if meter.reading_date else None
                ),
            }
            for kind, meter in data.meters.items()
        },
        "usage": {
            str(kind): _summarize_usage(days) for kind, days in data.usage.items()
        },
        "options": dict(entry.options),
        "prices": data.prices.as_dict(),
    }
