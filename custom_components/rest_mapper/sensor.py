"""Sensors: mapped fields, list items, item counts and endpoint status."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Final, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .coordinator import EndpointCoordinator, RestMapperConfigEntry
from .entity import ItemName, collection_device, endpoint_device, setup_items, setup_singles
from .lib import Position, Status
from .values import EventData

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .kinds import MappingConfig

PARALLEL_UPDATES = 0
_KIND: Final = "sensor"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors."""
    coordinator = entry.runtime_data
    async_add_entities(_status_sensors(coordinator))
    setup_singles(entry, async_add_entities, _KIND, MappedSensor)
    setup_items(entry, async_add_entities, _KIND, "sensor", MappedSensor)
    for mapping in coordinator.mappings.values():
        if mapping.list_path is not None:
            async_add_entities(
                [ItemCountSensor(coordinator, mapping)], config_subentry_id=mapping.subentry_id
            )


class MappedSensor(ItemName, SensorEntity):
    """A field or a list item."""

    def __init__(
        self,
        coordinator: EndpointCoordinator,
        mapping: MappingConfig,
        key: str | None = None,
    ) -> None:
        """Apply the mapping's settings."""
        super().__init__(coordinator, mapping, key)
        self._setup_identity()
        settings = mapping.settings
        device_class = settings.get("device_class")
        state_class = settings.get("state_class")
        unit = settings.get("unit_of_measurement")
        precision = settings.get("suggested_display_precision")
        if isinstance(device_class, str) and device_class:
            self._attr_device_class = SensorDeviceClass(device_class)
        if isinstance(state_class, str) and state_class:
            self._attr_state_class = SensorStateClass(state_class)
        if isinstance(unit, str) and unit:
            self._attr_native_unit_of_measurement = unit
        if isinstance(precision, int):
            self._attr_suggested_display_precision = precision

    @property
    @override
    def native_value(self) -> int | float | str | datetime | date | None:
        """Return the read value."""
        value = self.current.value
        if isinstance(value, bool):
            value = "true" if value else "false"
        if isinstance(value, Position | EventData):
            return None
        return self._fit(value)

    def _fit(
        self, value: float | str | datetime | date | None
    ) -> int | float | str | datetime | date | None:
        """Return only what the sensor's class accepts, as anything else raises on write."""
        device_class = self.device_class
        if device_class is SensorDeviceClass.TIMESTAMP:
            if isinstance(value, datetime):
                return value
            return dt_util.start_of_local_day(value) if isinstance(value, date) else None
        if device_class is SensorDeviceClass.DATE:
            if isinstance(value, datetime):
                return value.date()
            return value if isinstance(value, date) else None
        needs_number = (
            device_class is not None
            or self.state_class is not None
            or self.native_unit_of_measurement is not None
        )
        if needs_number and not isinstance(value, int | float):
            return None
        return value


class ItemCountSensor(CoordinatorEntity[EndpointCoordinator], SensorEntity):
    """The number of tracked items."""

    _attr_has_entity_name = True
    _attr_translation_key = "item_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EndpointCoordinator, mapping: MappingConfig) -> None:
        """Bind to a collection."""
        super().__init__(coordinator)
        self._subentry_id = mapping.subentry_id
        self._attr_unique_id = f"{mapping.subentry_id}_count"
        self._attr_device_info = collection_device(coordinator, mapping)

    @property
    @override
    def native_value(self) -> int:
        """Return the count."""
        collection = self.coordinator.data.collections.get(self._subentry_id)
        return len(collection.tracker.tracked) if collection is not None else 0


class StatusSensor(CoordinatorEntity[EndpointCoordinator], SensorEntity):
    """A diagnostic value of the endpoint."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EndpointCoordinator,
        key: str,
        read: Callable[[EndpointCoordinator], int | str | datetime | None],
    ) -> None:
        """Bind to the endpoint."""
        super().__init__(coordinator)
        self._read = read
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = endpoint_device(coordinator)

    @property
    @override
    def native_value(self) -> int | str | datetime | None:
        """Return the value."""
        return self._read(self.coordinator)


def _next_update(coordinator: EndpointCoordinator) -> datetime | None:
    """Return when the next request is due, as a state."""
    moment = coordinator.policy.next_request_at()
    return dt_util.as_utc(moment) if moment is not None else None


def _status_sensors(coordinator: EndpointCoordinator) -> list[StatusSensor]:
    """Build the endpoint's sensors."""
    last_data = StatusSensor(coordinator, "last_data", lambda item: item.data.confirmed_at)
    last_data._attr_device_class = SensorDeviceClass.TIMESTAMP
    status = StatusSensor(coordinator, "status", lambda item: item.status().value)
    status._attr_device_class = SensorDeviceClass.ENUM
    status._attr_options = [member.value for member in Status]
    next_update = StatusSensor(coordinator, "next_update", _next_update)
    next_update._attr_device_class = SensorDeviceClass.TIMESTAMP
    next_update._attr_entity_registry_enabled_default = False
    sensors = [last_data, status, next_update]
    if coordinator.policy.config.budget is not None:
        sensors.append(StatusSensor(coordinator, "requests_used", lambda item: item.policy.used))
        sensors.append(
            StatusSensor(coordinator, "requests_remaining", lambda item: item.policy.remaining)
        )
    return sensors
