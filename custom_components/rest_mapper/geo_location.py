"""Short-lived positions. They have no unique ID, so they never reach the registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.util.location import distance

from .entity import ItemName, setup_items
from .evaluation import is_placed
from .lib import Position
from .mappings import source_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry
    from .kinds import MappingConfig

PARALLEL_UPDATES = 0
_KIND: Final = "geo_location"
_METRES_IN_KILOMETRE: Final = 1000


def _placed_keys(coordinator: EndpointCoordinator, mapping: MappingConfig) -> set[str]:
    """Return items to show. Past the retention limit, positions leave the map."""
    collection = coordinator.data.collections.get(mapping.subentry_id)
    if collection is None or coordinator.expired:
        return set()
    return {key for key, read in collection.items.items() if is_placed(read)}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Follow position collections."""
    setup_items(entry, async_add_entities, _KIND, "geo_location", MappedPosition, _placed_keys)


class MappedPosition(ItemName, GeolocationEvent):
    """One item on the map."""

    _attr_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(self, coordinator: EndpointCoordinator, mapping: MappingConfig, key: str) -> None:
        """Bind to an item."""
        super().__init__(coordinator, mapping, key)
        self._attr_source = source_for(mapping)

    @property
    def _place(self) -> Position | None:
        """Return the read position."""
        value = self.current.value
        return value if isinstance(value, Position) else None

    @property
    @override
    def latitude(self) -> float | None:
        """Return the latitude."""
        place = self._place
        return place.latitude if place is not None else None

    @property
    @override
    def longitude(self) -> float | None:
        """Return the longitude."""
        place = self._place
        return place.longitude if place is not None else None

    @property
    @override
    def distance(self) -> float | None:
        """Return the distance from home."""
        place = self._place
        if place is None:
            return None
        config = self.hass.config
        metres = distance(config.latitude, config.longitude, place.latitude, place.longitude)
        return metres / _METRES_IN_KILOMETRE if metres is not None else None
