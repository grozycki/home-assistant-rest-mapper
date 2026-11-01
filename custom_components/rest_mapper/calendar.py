"""One calendar from a whole list."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final, override

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .entity import collection_device
from .kinds import Shape
from .values import EventData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry
    from .kinds import MappingConfig

PARALLEL_UPDATES = 0
_KIND: Final = "calendar"
_ALL_DAY: Final = timedelta(days=1)
_TIMED: Final = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RestMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add calendars."""
    coordinator = entry.runtime_data
    for mapping in coordinator.mappings.values():
        if mapping.kind == _KIND and mapping.shape is Shape.WHOLE_LIST:
            async_add_entities(
                [MappedCalendar(coordinator, mapping)], config_subentry_id=mapping.subentry_id
            )


def _build_event(key: str, data: EventData) -> CalendarEvent:
    """Convert an event, filling in a missing or impossible end."""
    start = data.start
    end = data.end
    fallback = _TIMED if isinstance(start, datetime) else _ALL_DAY
    if end is None or type(end) is not type(start) or end <= start:
        end = start + fallback
    return CalendarEvent(
        start=start,
        end=end,
        summary=data.summary,
        description=data.description,
        location=data.location,
        uid=key,
    )


def _as_time(moment: datetime | date) -> datetime:
    """Compare dates as local midnight."""
    if isinstance(moment, datetime):
        return moment
    return dt_util.start_of_local_day(moment)


class MappedCalendar(CoordinatorEntity["EndpointCoordinator"], CalendarEntity):
    """All events come from stored data."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: EndpointCoordinator, mapping: MappingConfig) -> None:
        """Bind to a collection."""
        super().__init__(coordinator)
        self._subentry_id = mapping.subentry_id
        self._attr_unique_id = mapping.subentry_id
        self._attr_device_info = collection_device(coordinator, mapping)

    def _events(self) -> list[CalendarEvent]:
        """Return events in order of start."""
        collection = self.coordinator.data.collections.get(self._subentry_id)
        if collection is None:
            return []
        events = [
            _build_event(key, read.value)
            for key, read in collection.items.items()
            if isinstance(read.value, EventData)
        ]
        return sorted(events, key=lambda event: _as_time(event.start))

    @property
    @override
    def available(self) -> bool:
        """Stay available through failures, up to the retention limit."""
        return not self.coordinator.expired

    @property
    @override
    def event(self) -> CalendarEvent | None:
        """Return the current or next event."""
        now = dt_util.utcnow()
        for event in self._events():
            if _as_time(event.end) > now:
                return event
        return None

    @override
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return events in a range. This sends no request."""
        return [
            event
            for event in self._events()
            if _as_time(event.end) > start_date and _as_time(event.start) < end_date
        ]
