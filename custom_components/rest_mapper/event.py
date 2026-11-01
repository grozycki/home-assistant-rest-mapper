"""An event for each new list item."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_KEY, ATTR_LEFT_OUT, EVENT_NEW_ITEM, MAX_EVENTS_PER_RESPONSE
from .entity import collection_device

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry
    from .kinds import MappingConfig

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add event entities."""
    coordinator = entry.runtime_data
    for mapping in coordinator.mappings.values():
        if mapping.settings.get("events") is True:
            async_add_entities(
                [NewItemEvent(coordinator, mapping)], config_subentry_id=mapping.subentry_id
            )


class NewItemEvent(CoordinatorEntity["EndpointCoordinator"], EventEntity):
    """Fires for items reported as added."""

    _attr_has_entity_name = True
    _attr_translation_key = "new_item"

    def __init__(self, coordinator: EndpointCoordinator, mapping: MappingConfig) -> None:
        """Bind to a collection."""
        super().__init__(coordinator)
        self._subentry_id = mapping.subentry_id
        self._attr_unique_id = f"{mapping.subentry_id}_new_item"
        self._attr_event_types = [EVENT_NEW_ITEM]
        self._attr_device_info = collection_device(coordinator, mapping)

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Fire once for each added item, up to the limit."""
        collection = self.coordinator.data.collections.get(self._subentry_id)
        if collection is None or not collection.added:
            return
        added = collection.added
        shown = added[:MAX_EVENTS_PER_RESPONSE]
        for index, key in enumerate(shown):
            read = collection.items.get(key)
            attributes: dict[str, object] = dict(read.attributes) if read is not None else {}
            attributes[ATTR_KEY] = key
            if index == len(shown) - 1 and len(added) > len(shown):
                attributes[ATTR_LEFT_OUT] = len(added) - len(shown)
            self._trigger_event(EVENT_NEW_ITEM, attributes)
            self.async_write_ha_state()
        collection.added = ()
