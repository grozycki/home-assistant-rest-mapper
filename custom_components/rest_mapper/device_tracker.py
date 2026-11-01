"""Trackers: positions of mapped fields or list items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from homeassistant.components.device_tracker.entity import TrackerEntity

from .entity import ItemName, setup_items, setup_singles
from .lib import Position

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry
    from .kinds import MappingConfig

PARALLEL_UPDATES = 0
_KIND: Final = "device_tracker"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add trackers."""
    setup_singles(entry, async_add_entities, _KIND, MappedTracker)
    setup_items(entry, async_add_entities, _KIND, "device_tracker", MappedTracker)


class MappedTracker(ItemName, TrackerEntity):
    """A position. Here it is main data, so it is not diagnostic."""

    _attr_entity_category = None  # type: ignore[assignment]  # The base fixes diagnostic.

    def __init__(
        self,
        coordinator: EndpointCoordinator,
        mapping: MappingConfig,
        key: str | None = None,
    ) -> None:
        """Set identity."""
        super().__init__(coordinator, mapping, key)
        self._setup_identity()

    @property
    @override
    def force_update(self) -> bool:
        """Write only changes, as `304` responses would otherwise fill history."""
        return False

    @property
    def _position(self) -> Position | None:
        """Return the read position."""
        value = self.current.value
        return value if isinstance(value, Position) else None

    @property
    @override
    def latitude(self) -> float | None:
        """Return the latitude."""
        position = self._position
        return position.latitude if position is not None else None

    @property
    @override
    def longitude(self) -> float | None:
        """Return the longitude."""
        position = self._position
        return position.longitude if position is not None else None

    @property
    @override
    def location_accuracy(self) -> float:
        """Return the accuracy in metres."""
        position = self._position
        return position.accuracy if position is not None else 0.0
