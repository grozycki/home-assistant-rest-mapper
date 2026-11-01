"""The refresh button of an endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity import endpoint_device

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the button."""
    async_add_entities([RefreshButton(entry.runtime_data)])


class RefreshButton(CoordinatorEntity["EndpointCoordinator"], ButtonEntity):
    """Requests one fetch in any mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"

    def __init__(self, coordinator: EndpointCoordinator) -> None:
        """Bind to the endpoint."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_refresh"
        self._attr_device_info = endpoint_device(coordinator)

    @override
    async def async_press(self) -> None:
        """Fetch, or fail with the reason."""
        await self.coordinator.async_manual_refresh()
