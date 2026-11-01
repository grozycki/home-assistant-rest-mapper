"""The list of entity types. Adding a type starts with a row in `KINDS`.

This module imports no Home Assistant platform, so a type can target a newer version.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, tzinfo
from enum import StrEnum
from typing import Final, final

from awesomeversion import AwesomeVersion
from homeassistant.const import __version__ as HA_VERSION

from .lib import (
    Failure,
    GeometryPath,
    JsonValue,
    LatLonPaths,
    Path,
    Position,
    ValueSpec,
    ValueType,
    read_field,
    read_position,
)
from .values import EventData, ReadValue


class Shape(StrEnum):
    """What one mapping of a kind stands for."""

    SINGLE = "single"
    ITEM = "item"
    WHOLE_LIST = "whole_list"


@final
@dataclass(frozen=True, slots=True)
class MappingConfig:
    """One mapping, as stored in a subentry."""

    subentry_id: str
    title: str
    kind: str
    shape: Shape
    roles: dict[str, Path]
    settings: dict[str, JsonValue]
    attributes: dict[str, Path]
    fingerprint: str
    list_path: Path | None = None
    key_path: Path | None = None


type Reader = Callable[[JsonValue, MappingConfig, tzinfo], ReadValue | Failure]
type Validator = Callable[[Mapping[str, str]], str | None]


@final
@dataclass(frozen=True, slots=True)
class Role:
    """A path a kind reads."""

    name: str
    required: bool = True


@final
@dataclass(frozen=True, slots=True)
class KindDefinition:
    """Everything the flows, storage and platforms need to know about a type."""

    name: str
    platform: str
    shapes: frozenset[Shape]
    roles: tuple[Role, ...]
    read: Reader
    validate: Validator | None = None
    min_version: str = "2026.10.0"
    has_attributes: bool = True
    settings: tuple[str, ...] = field(default=())

    def available(self) -> bool:
        """Tell whether the running Home Assistant has this platform."""
        return AwesomeVersion(HA_VERSION) >= AwesomeVersion(self.min_version)


def _text_set(value: JsonValue) -> frozenset[str]:
    """Read on or off values from their comma-separated form."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(part.strip().casefold() for part in value.split(",") if part.strip())


def _time_zone(mapping: MappingConfig, local: tzinfo) -> tzinfo:
    """Return the zone for times with none."""
    return local if mapping.settings.get("naive_local") is True else UTC


def read_sensor(document: JsonValue, mapping: MappingConfig, local: tzinfo) -> ReadValue | Failure:
    """Read a sensor's value."""
    value_type = mapping.settings.get("value_type")
    spec = ValueSpec(
        ValueType(value_type) if value_type in tuple(ValueType) else ValueType.TEXT,
        naive_timezone=_time_zone(mapping, local),
    )
    return read_field(document, mapping.roles["value"], spec)


def read_binary(document: JsonValue, mapping: MappingConfig, _local: tzinfo) -> ReadValue | Failure:
    """Read a binary sensor's value."""
    spec = ValueSpec(
        ValueType.BOOLEAN,
        on_values=_text_set(mapping.settings.get("on_values")),
        off_values=_text_set(mapping.settings.get("off_values")),
    )
    return read_field(document, mapping.roles["value"], spec)


def read_place(document: JsonValue, mapping: MappingConfig, _local: tzinfo) -> Position | Failure:
    """Read a position from latitude and longitude, or a geometry."""
    roles = mapping.roles
    accuracy = roles.get("accuracy")
    if "geometry" in roles:
        return read_position(document, GeometryPath(roles["geometry"], accuracy))
    return read_position(document, LatLonPaths(roles["latitude"], roles["longitude"], accuracy))


def read_event(document: JsonValue, mapping: MappingConfig, local: tzinfo) -> EventData | Failure:
    """Read a calendar event."""
    moment = ValueSpec(ValueType.TIMESTAMP, naive_timezone=_time_zone(mapping, local))
    text = ValueSpec(ValueType.TEXT)
    start = read_field(document, mapping.roles["start"], moment)
    summary = read_field(document, mapping.roles["summary"], text)
    if isinstance(start, Failure):
        return start
    if isinstance(summary, Failure):
        return summary
    if isinstance(start, date) and isinstance(summary, str):
        end = _optional(document, mapping, "end", moment)
        return EventData(
            start,
            end if isinstance(end, date) else None,
            summary,
            _optional_text(document, mapping, "description"),
            _optional_text(document, mapping, "location"),
        )
    return Failure.TYPE_MISMATCH


def _optional(document: JsonValue, mapping: MappingConfig, role: str, spec: ValueSpec) -> ReadValue:
    """Read an optional role, with `None` when unreadable."""
    path = mapping.roles.get(role)
    if path is None:
        return None
    value = read_field(document, path, spec)
    return None if isinstance(value, Failure) else value


def _optional_text(document: JsonValue, mapping: MappingConfig, role: str) -> str | None:
    """Read an optional text role."""
    value = _optional(document, mapping, role, ValueSpec(ValueType.TEXT))
    return value if isinstance(value, str) else None


def validate_position(roles: Mapping[str, str]) -> str | None:
    """Require a geometry, or both latitude and longitude."""
    if roles.get("geometry") or (roles.get("latitude") and roles.get("longitude")):
        return None
    return "position_required"


_POSITION_ROLES: Final = (
    Role("latitude", required=False),
    Role("longitude", required=False),
    Role("geometry", required=False),
    Role("accuracy", required=False),
)

KINDS: Final[dict[str, KindDefinition]] = {
    "sensor": KindDefinition(
        "sensor",
        "sensor",
        frozenset({Shape.SINGLE, Shape.ITEM}),
        (Role("value"),),
        read_sensor,
        settings=(
            "value_type",
            "device_class",
            "unit_of_measurement",
            "state_class",
            "suggested_display_precision",
            "naive_local",
        ),
    ),
    "binary_sensor": KindDefinition(
        "binary_sensor",
        "binary_sensor",
        frozenset({Shape.SINGLE, Shape.ITEM}),
        (Role("value"),),
        read_binary,
        settings=("device_class", "on_values", "off_values"),
    ),
    "device_tracker": KindDefinition(
        "device_tracker",
        "device_tracker",
        frozenset({Shape.SINGLE, Shape.ITEM}),
        _POSITION_ROLES,
        read_place,
        validate_position,
    ),
    "geo_location": KindDefinition(
        "geo_location",
        "geo_location",
        frozenset({Shape.ITEM}),
        _POSITION_ROLES,
        read_place,
        validate_position,
    ),
    "calendar": KindDefinition(
        "calendar",
        "calendar",
        frozenset({Shape.WHOLE_LIST}),
        (
            Role("start"),
            Role("summary"),
            Role("end", required=False),
            Role("description", required=False),
            Role("location", required=False),
        ),
        read_event,
        has_attributes=False,
        settings=("naive_local",),
    ),
}
