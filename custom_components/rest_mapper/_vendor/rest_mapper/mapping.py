"""Read typed values from a response, and say why a read failed."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from enum import StrEnum
from typing import Final, assert_never, final

from .paths import JsonValue, Missing, Path, resolve

MAX_TEXT_LENGTH: Final = 255
MAX_ATTRIBUTE_BYTES: Final = 1024
_MILLISECONDS_FROM: Final = 100_000_000_000
_DEFAULT_ON: Final = frozenset({"true", "1", "on", "yes"})
_DEFAULT_OFF: Final = frozenset({"false", "0", "off", "no"})
_LATITUDE_LIMIT: Final = 90.0
_LONGITUDE_LIMIT: Final = 180.0
_DATE_LENGTH: Final = 10
_VERTEX_LENGTH: Final = 2

type Scalar = int | float | str | bool | datetime | date


class ValueType(StrEnum):
    """How a field is read."""

    NUMBER = "number"
    TEXT = "text"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"


class Failure(StrEnum):
    """Why a value could not be read."""

    MISSING = "missing"
    NULL = "null"
    TYPE_MISMATCH = "type_mismatch"


@final
@dataclass(frozen=True, slots=True)
class ValueSpec:
    """How to read one field."""

    type: ValueType
    on_values: frozenset[str] = frozenset()
    off_values: frozenset[str] = frozenset()
    naive_timezone: tzinfo = UTC


@final
@dataclass(frozen=True, slots=True)
class Position:
    """A place on Earth."""

    latitude: float
    longitude: float
    accuracy: float = 0.0


@final
@dataclass(frozen=True, slots=True)
class LatLonPaths:
    """A position read from two fields."""

    latitude: Path
    longitude: Path
    accuracy: Path | None = None


@final
@dataclass(frozen=True, slots=True)
class GeometryPath:
    """A position read from a GeoJSON geometry."""

    geometry: Path
    accuracy: Path | None = None


type PositionSpec = LatLonPaths | GeometryPath


def read_field(data: JsonValue, path: Path, spec: ValueSpec) -> Scalar | Failure:
    """Read a field. This returns a failure for any input, and never raises."""
    value = resolve(data, path)
    if isinstance(value, Missing):
        return Failure.MISSING
    if value is None:
        return Failure.NULL
    match spec.type:
        case ValueType.NUMBER:
            return _read_number(value)
        case ValueType.TEXT:
            return _read_text(value)
        case ValueType.TIMESTAMP:
            return _read_timestamp(value, spec.naive_timezone)
        case ValueType.BOOLEAN:
            return _read_boolean(value, spec)
        case _ as unreachable:
            assert_never(unreachable)


def _read_number(value: JsonValue) -> int | float | Failure:
    """Accept finite numbers and text holding one."""
    if isinstance(value, bool):
        return Failure.TYPE_MISMATCH
    if isinstance(value, int):
        return value if abs(value) <= sys.float_info.max else Failure.TYPE_MISMATCH
    if isinstance(value, float):
        return value if math.isfinite(value) else Failure.TYPE_MISMATCH
    if isinstance(value, str) and "_" not in value:
        try:
            number = float(value)
        except ValueError:
            return Failure.TYPE_MISMATCH
        return number if math.isfinite(number) else Failure.TYPE_MISMATCH
    return Failure.TYPE_MISMATCH


def _read_text(value: JsonValue) -> str | Failure:
    """Accept scalars, limited to the length of a state."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value[:MAX_TEXT_LENGTH]
    if isinstance(value, int | float):
        return str(value)
    return Failure.TYPE_MISMATCH


def _read_timestamp(value: JsonValue, naive_timezone: tzinfo) -> datetime | date | Failure:
    """Accept ISO 8601 text, or Unix time in seconds or milliseconds."""
    if isinstance(value, bool):
        return Failure.TYPE_MISMATCH
    if isinstance(value, int | float):
        number = _read_number(value)
        return number if isinstance(number, Failure) else _from_unix(number)
    if not isinstance(value, str):
        return Failure.TYPE_MISMATCH
    text = value.strip()
    try:
        if len(text) == _DATE_LENGTH:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return Failure.TYPE_MISMATCH
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=naive_timezone)


def _from_unix(value: float) -> datetime | Failure:
    """Convert Unix time, telling milliseconds by their size."""
    seconds = value / 1000 if abs(value) >= _MILLISECONDS_FROM else value
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return Failure.TYPE_MISMATCH


def normalize_boolean_text(value: JsonValue) -> str | None:
    """Return the form in which on and off values are compared."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float | str):
        return str(value).strip().casefold()
    return None


def _read_boolean(value: JsonValue, spec: ValueSpec) -> bool | Failure:
    """Compare with the mapping's own values or the defaults.

    With own values on one side only, everything else is the other side.
    """
    text = normalize_boolean_text(value)
    if text is None:
        return Failure.TYPE_MISMATCH
    if not spec.on_values and not spec.off_values:
        on_values, off_values = _DEFAULT_ON, _DEFAULT_OFF
    else:
        on_values, off_values = spec.on_values, spec.off_values
    if text in on_values:
        return True
    if text in off_values:
        return False
    if on_values and not off_values:
        return False
    if off_values and not on_values:
        return True
    return Failure.TYPE_MISMATCH


def read_position(data: JsonValue, spec: PositionSpec) -> Position | Failure:
    """Read a position from two fields or a GeoJSON geometry."""
    latitude: float | Failure
    longitude: float | Failure
    if isinstance(spec, LatLonPaths):
        latitude = _coordinate(data, spec.latitude)
        longitude = _coordinate(data, spec.longitude)
    else:
        pair = _read_geometry(resolve(data, spec.geometry))
        if isinstance(pair, Failure):
            return pair
        longitude, latitude = pair
    if isinstance(latitude, Failure):
        return latitude
    if isinstance(longitude, Failure):
        return longitude
    if abs(latitude) > _LATITUDE_LIMIT or abs(longitude) > _LONGITUDE_LIMIT:
        return Failure.TYPE_MISMATCH
    return Position(latitude, longitude, _accuracy(data, spec.accuracy))


def _coordinate(data: JsonValue, path: Path) -> float | Failure:
    """Read one coordinate."""
    value = resolve(data, path)
    if isinstance(value, Missing):
        return Failure.MISSING
    if value is None:
        return Failure.NULL
    number = _read_number(value)
    return number if isinstance(number, Failure) else float(number)


def _read_geometry(value: JsonValue | Missing) -> tuple[float, float] | Failure:
    """Return longitude and latitude, in GeoJSON order."""
    if isinstance(value, Missing):
        return Failure.MISSING
    if value is None:
        return Failure.NULL
    if not isinstance(value, dict):
        return Failure.TYPE_MISMATCH
    coordinates = value.get("coordinates")
    match value.get("type"):
        case "Point":
            return _vertex(coordinates)
        case "LineString":
            if isinstance(coordinates, list) and coordinates:
                return _vertex(coordinates[len(coordinates) // 2])
            return Failure.TYPE_MISMATCH
        case _:
            return Failure.TYPE_MISMATCH


def _vertex(value: JsonValue) -> tuple[float, float] | Failure:
    """Read one GeoJSON vertex, which may also hold an altitude."""
    if not isinstance(value, list) or len(value) < _VERTEX_LENGTH:
        return Failure.TYPE_MISMATCH
    longitude = _read_number(value[0])
    latitude = _read_number(value[1])
    if isinstance(longitude, Failure) or isinstance(latitude, Failure):
        return Failure.TYPE_MISMATCH
    return float(longitude), float(latitude)


def _accuracy(data: JsonValue, path: Path | None) -> float:
    """Read an optional accuracy, which falls back to zero."""
    if path is None:
        return 0.0
    value = resolve(data, path)
    if isinstance(value, Missing) or value is None:
        return 0.0
    number = _read_number(value)
    return 0.0 if isinstance(number, Failure) or number < 0 else float(number)


@final
@dataclass(frozen=True, slots=True)
class AttributeReads:
    """Attributes that could be read, and those left out for size."""

    values: dict[str, JsonValue] = field(default_factory=dict)
    too_large: tuple[str, ...] = ()


def read_attributes(data: JsonValue, paths: dict[str, Path]) -> AttributeReads:
    """Read attributes by name. Missing paths are left out."""
    values: dict[str, JsonValue] = {}
    too_large: list[str] = []
    for name, path in paths.items():
        value = resolve(data, path)
        if isinstance(value, Missing):
            continue
        if len(json.dumps(value).encode()) > MAX_ATTRIBUTE_BYTES:
            too_large.append(name)
        else:
            values[name] = value
    return AttributeReads(values, tuple(too_large))
