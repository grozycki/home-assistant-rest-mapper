"""Apply a new response to what is known. Pure, so it can run in the executor."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, final

from .const import ROLE_NAME
from .kinds import KINDS, MappingConfig, Shape
from .lib import (
    DEFAULT_ITEM_LIMIT,
    DEFAULT_REMOVAL_AFTER,
    Failure,
    ItemTracker,
    JsonValue,
    Position,
    ValueSpec,
    ValueType,
    read_attributes,
    read_field,
    read_items,
)
from .values import ValueRead

if TYPE_CHECKING:
    from datetime import datetime, tzinfo


@final
@dataclass(slots=True)
class CollectionRead:
    """What is known about one collection."""

    tracker: ItemTracker
    items: dict[str, ValueRead] = field(default_factory=dict)
    added: tuple[str, ...] = ()
    skipped: int = 0
    duplicates: int = 0
    ignored: int = 0
    unreadable: int = 0
    listed: int = 0
    list_failures: int = 0

    def to_data(self) -> dict[str, JsonValue]:
        """Return plain data for storage."""
        return {
            "tracker": self.tracker.to_data(),
            "items": {key: read.to_data() for key, read in self.items.items()},
            "list_failures": self.list_failures,
        }


def copy_collection(mapping: MappingConfig, collection: CollectionRead) -> CollectionRead:
    """Return a copy to change, while entities keep reading the original."""
    tracker = new_tracker(mapping)
    tracker.restore(collection.tracker.to_data())
    return CollectionRead(
        tracker,
        dict(collection.items),
        (),
        collection.skipped,
        collection.duplicates,
        collection.ignored,
        collection.unreadable,
        collection.listed,
        collection.list_failures,
    )


def new_tracker(mapping: MappingConfig) -> ItemTracker:
    """Create a tracker with the mapping's limits."""
    limit = mapping.settings.get("item_limit")
    removal = mapping.settings.get("removal_after")
    return ItemTracker(
        limit if isinstance(limit, int) else DEFAULT_ITEM_LIMIT,
        removal if isinstance(removal, int) else DEFAULT_REMOVAL_AFTER,
    )


def collection_from_data(mapping: MappingConfig, data: JsonValue) -> CollectionRead:
    """Read stored data."""
    tracker = new_tracker(mapping)
    collection = CollectionRead(tracker)
    if not isinstance(data, dict):
        return collection
    tracker_data = data.get("tracker")
    if isinstance(tracker_data, dict):
        tracker.restore(tracker_data)
    items = data.get("items")
    if isinstance(items, dict):
        collection.items = {key: ValueRead.from_data(item) for key, item in items.items()}
    failures = data.get("list_failures")
    collection.list_failures = failures if isinstance(failures, int) else 0
    return collection


def read_value(
    document: JsonValue,
    mapping: MappingConfig,
    previous: ValueRead,
    now: datetime,
    local: tzinfo,
) -> ValueRead:
    """Read one value, keeping the last good one when it fails."""
    result = KINDS[mapping.kind].read(document, mapping, local)
    attributes = read_attributes(document, mapping.attributes).values
    name = _read_name(document, mapping) or previous.name
    if result is Failure.NULL:
        return ValueRead(None, now, attributes, 0, None, name)
    if isinstance(result, Failure):
        return replace(
            previous,
            attributes=attributes,
            failures=previous.failures + 1,
            failure=result,
            name=name,
        )
    return ValueRead(result, now, attributes, 0, None, name)


def _read_name(document: JsonValue, mapping: MappingConfig) -> str | None:
    """Read an item's name, if it has one."""
    path = mapping.roles.get(ROLE_NAME)
    if path is None:
        return None
    name = read_field(document, path, ValueSpec(ValueType.TEXT))
    return name if isinstance(name, str) and name else None


def apply_to_collection(
    document: JsonValue,
    mapping: MappingConfig,
    collection: CollectionRead,
    now: datetime,
    local: tzinfo,
) -> None:
    """Apply a new response to a collection."""
    collection.added = ()
    if mapping.list_path is None or mapping.key_path is None:
        return
    items = read_items(document, mapping.list_path, mapping.key_path)
    if items is None:
        collection.list_failures += 1
        return
    collection.list_failures = 0
    changes = collection.tracker.update(tuple(items.items))
    tracked = set(collection.tracker.tracked)
    for key in changes.removed:
        collection.items.pop(key, None)
    collection.added = changes.added
    collection.skipped = items.skipped
    collection.duplicates = items.duplicates
    collection.ignored = changes.ignored
    collection.listed = len(items.items) + items.skipped
    unreadable = 0
    for key, item in items.items.items():
        if key not in tracked:
            continue
        previous = collection.items.get(key, ValueRead())
        read = read_value(item, mapping, previous, now, local)
        if read.value is None and is_placed(previous):
            read.value = previous.value
        if read.failure is not None:
            unreadable += 1
        collection.items[key] = read
    collection.unreadable = unreadable


def confirm(read: ValueRead, now: datetime) -> None:
    """Note that a `304` confirmed a value."""
    if read.failure is None and read.confirmed_at is not None:
        read.confirmed_at = now


def is_placed(read: ValueRead) -> bool:
    """Tell whether an item has a position."""
    return isinstance(read.value, Position)


def is_collection(mapping: MappingConfig) -> bool:
    """Tell whether a mapping reads a list."""
    return mapping.shape is not Shape.SINGLE
