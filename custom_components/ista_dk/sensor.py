"""Sensor platform for ista Denmark."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_PERIOD_END,
    ATTR_PERIOD_START,
    ATTR_READING_DATE,
    ATTR_SOURCE,
    DOMAIN,
    MeterKind,
)
from .coordinator import IstaDkConfigEntry, IstaDkCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class IstaDkPriceDescription(SensorEntityDescription):
    """Describes the unit price sensor of a meter kind."""

    kind: MeterKind
    per_unit: str


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=MeterKind.COLD_WATER,
        translation_key=MeterKind.COLD_WATER,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key=MeterKind.HOT_WATER,
        translation_key=MeterKind.HOT_WATER,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key=MeterKind.HEAT,
        translation_key=MeterKind.HEAT,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
    ),
)

PRICE_DESCRIPTIONS: tuple[IstaDkPriceDescription, ...] = (
    IstaDkPriceDescription(
        key="cold_water_price",
        translation_key="cold_water_price",
        kind=MeterKind.COLD_WATER,
        per_unit=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
    ),
    IstaDkPriceDescription(
        key="hot_water_price",
        translation_key="hot_water_price",
        kind=MeterKind.HOT_WATER,
        per_unit=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
    ),
    IstaDkPriceDescription(
        key="heat_price",
        translation_key="heat_price",
        kind=MeterKind.HEAT,
        per_unit=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=4,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IstaDkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one sensor per meter and a price sensor once a price is known."""
    coordinator = entry.runtime_data
    async_add_entities(
        IstaDkSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
        if MeterKind(description.key) in coordinator.data.meters
    )

    added_prices: set[str] = set()

    @callback
    def _async_add_price_sensors() -> None:
        new = [
            description
            for description in PRICE_DESCRIPTIONS
            if description.key not in added_prices
            and description.kind in coordinator.data.meters
            and coordinator.data.prices.current(description.kind) is not None
        ]
        added_prices.update(description.key for description in new)
        async_add_entities(
            IstaDkPriceSensor(coordinator, description) for description in new
        )

    _async_add_price_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_price_sensors))


class IstaDkEntity(CoordinatorEntity[IstaDkCoordinator], SensorEntity):
    """Base class of the ista Denmark sensors, all on one service device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: IstaDkCoordinator, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.cons_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.cons_id)},
            name="ista",
            manufacturer="ista",
            model="istaonline.dk",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.istaonline.dk",
        )


class IstaDkSensor(IstaDkEntity):
    """Cumulative register reading of one ista meter."""

    def __init__(
        self, coordinator: IstaDkCoordinator, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._kind = MeterKind(description.key)

    @property
    def native_value(self) -> float | None:
        """Return the meter's cumulative register."""
        if (meter := self.coordinator.data.meters.get(self._kind)) is None:
            return None
        return meter.reading

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the date of the latest reading."""
        if (meter := self.coordinator.data.meters.get(self._kind)) is None:
            return None
        return {
            ATTR_READING_DATE: meter.reading_date.isoformat()
            if meter.reading_date
            else None
        }


class IstaDkPriceSensor(IstaDkEntity):
    """Unit price of one meter kind for the open period."""

    entity_description: IstaDkPriceDescription

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the currency per unit of the meter."""
        return f"{self.hass.config.currency}/{self.entity_description.per_unit}"

    @property
    def native_value(self) -> float | None:
        """Return the unit price."""
        current = self.coordinator.data.prices.current(self.entity_description.kind)
        return current.price if current else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose where the price comes from."""
        current = self.coordinator.data.prices.current(self.entity_description.kind)
        if current is None:
            return None
        return {
            ATTR_SOURCE: current.source,
            ATTR_PERIOD_START: (
                current.period_start.isoformat() if current.period_start else None
            ),
            ATTR_PERIOD_END: (
                current.period_end.isoformat() if current.period_end else None
            ),
        }
