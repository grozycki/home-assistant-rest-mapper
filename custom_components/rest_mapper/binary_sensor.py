"""Binary sensors: mapped booleans, and whether an endpoint's data is doubtful."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity import ItemName, endpoint_device, setup_items, setup_singles

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry
    from .kinds import MappingConfig

PARALLEL_UPDATES = 0
_KIND: Final = "binary_sensor"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add binary sensors."""
    async_add_entities([StaleSensor(entry.runtime_data)])
    setup_singles(entry, async_add_entities, _KIND, MappedBinarySensor)
    setup_items(entry, async_add_entities, _KIND, "binary_sensor", MappedBinarySensor)


class MappedBinarySensor(ItemName, BinarySensorEntity):
    """A boolean field or list item."""

    def __init__(
        self,
        coordinator: EndpointCoordinator,
        mapping: MappingConfig,
        key: str | None = None,
    ) -> None:
        """Apply the mapping's settings."""
        super().__init__(coordinator, mapping, key)
        self._setup_identity()
        device_class = mapping.settings.get("device_class")
        if isinstance(device_class, str) and device_class:
            self._attr_device_class = BinarySensorDeviceClass(device_class)

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the read value."""
        value = self.current.value
        return value if isinstance(value, bool) else None


class StaleSensor(CoordinatorEntity["EndpointCoordinator"], BinarySensorEntity):
    """On when data is too old or the last automatic request failed."""

    _attr_has_entity_name = True
    _attr_translation_key = "stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EndpointCoordinator) -> None:
        """Bind to the endpoint."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_stale"
        self._attr_device_info = endpoint_device(coordinator)

    @property
    @override
    def is_on(self) -> bool:
        """Return whether data is doubtful."""
        return self.coordinator.stale
