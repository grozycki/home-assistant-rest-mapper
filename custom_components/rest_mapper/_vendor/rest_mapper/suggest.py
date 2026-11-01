"""Work out a mapping from what a response holds.

Rules read names and values only, so they know no particular API. A wrong guess costs
one correction, so simple scoring is enough.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from enum import StrEnum
from typing import Final, final

from yarl import URL

from .flatten import CollectionCandidate, FlatField, ScalarType
from .mapping import ValueSpec, ValueType, read_field
from .paths import JsonValue, Missing, Path, Segment, parse_path, resolve

MAX_LABEL_PARTS: Final = 2
LABEL_SEPARATOR: Final = " \u203a "
_LATITUDE_LIMIT: Final = 90.0
_LONGITUDE_LIMIT: Final = 180.0
_KEY_NAMES: Final = frozenset({"id", "uuid", "guid", "key", "hex", "code", "slug", "identyfikator"})
_NAME_NAMES: Final = frozenset({"name", "title", "label", "nazwa", "tytul", "summary"})
_TEXT_NAMES: Final = frozenset({"description", "desc", "opis", "flight", "callsign"})
_LATITUDE_NAMES: Final = frozenset({"lat", "latitude", "szerokosc"})
_LONGITUDE_NAMES: Final = frozenset({"lon", "lng", "long", "longitude", "dlugosc"})
_DATE_NAMES: Final = frozenset({"date", "start", "starts", "begin", "when", "data", "termin"})
_END_NAMES: Final = frozenset({"end", "ends", "until", "koniec"})
_CREDENTIAL_PARAMETERS: Final = frozenset(
    {"key", "api_key", "apikey", "api-key", "token", "access_token", "subscription-key"}
)
_WORD: Final = re.compile(r"[^\W\d_]+|\d+")
_CAMEL_CASE: Final = re.compile(r"(?<=[^\W\d_])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])")
_IDENTIFIER: Final = re.compile(r"[0-9a-fA-F-]{16,}|\S*\d\S*-\S*\d\S*")


class EntityKind(StrEnum):
    """What a mapping can become."""

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    DEVICE_TRACKER = "device_tracker"
    GEO_LOCATION = "geo_location"
    CALENDAR = "calendar"


@final
@dataclass(frozen=True, slots=True)
class PositionPaths:
    """Where an item's position is."""

    geometry: str | None = None
    latitude: str | None = None
    longitude: str | None = None


@final
@dataclass(frozen=True, slots=True)
class CollectionSuggestion:
    """A proposed mapping of a list."""

    kind: EntityKind
    key: str | None
    name: str | None
    position: PositionPaths | None
    start: str | None
    end: str | None
    value: str | None
    example: str


@final
@dataclass(frozen=True, slots=True)
class FieldSuggestion:
    """A proposed mapping of one field."""

    kind: EntityKind
    name: str
    value_type: ValueType
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None


_FIELD_CLASSES: Final[dict[str, tuple[str, str]]] = {
    "temp": ("temperature", "°C"),
    "temperature": ("temperature", "°C"),
    "temperatura": ("temperature", "°C"),
    "humidity": ("humidity", "%"),
    "wilgotnosc": ("humidity", "%"),
    "pressure": ("atmospheric_pressure", "hPa"),
    "cisnienie": ("atmospheric_pressure", "hPa"),
    "battery": ("battery", "%"),
    "bateria": ("battery", "%"),
}


def words_of(text: str) -> list[str]:
    """Split a name into lowercase words."""
    return [word.casefold() for word in _WORD.findall(_CAMEL_CASE.sub(" ", text))]


def _named_parts(segments: tuple[Segment, ...]) -> list[str]:
    """Return the named parts of a path."""
    return [segment for segment in segments if isinstance(segment, str)]


def _parse(text: str) -> Path:
    """Parse an absolute or relative path."""
    return parse_path(text, relative=not text.startswith("$"))


def label_for(path: str) -> str:
    """Return a readable label for a path."""
    parts = _named_parts(_parse(path).segments)[-MAX_LABEL_PARTS:]
    readable = [" ".join(words_of(part)).capitalize() or part for part in parts]
    return LABEL_SEPARATOR.join(readable) or path


def name_for(path: str) -> str:
    """Return a readable name for an entity from a path."""
    parts = _named_parts(_parse(path).segments)
    return " ".join(words_of(parts[-1])).capitalize() if parts else "Value"


def _last_name(path: str) -> str:
    """Return the last named part in lowercase."""
    parts = _named_parts(_parse(path).segments)
    return "".join(words_of(parts[-1])) if parts else ""


def _looks_like_identifier(text: str) -> bool:
    """Tell an identifier or code from words."""
    return _IDENTIFIER.fullmatch(text) is not None or not any(c.isalpha() for c in text)


def _key_score(path: str) -> int:
    """Rate a path as a key."""
    name = _last_name(path)
    words = words_of(_named_parts(_parse(path).segments)[-1]) if name else []
    if name in _KEY_NAMES:
        return 3
    if words and words[-1] in _KEY_NAMES:
        return 2
    if words and words[-1] in _NAME_NAMES | _DATE_NAMES | {"time", "at"}:
        return 0
    return 1


def _suggest_key(candidate: CollectionCandidate) -> str | None:
    """Pick the best key."""
    ranked = sorted(candidate.likely_keys, key=_key_score, reverse=True)
    return ranked[0] if ranked else None


def _values(items: list[JsonValue], path: str) -> list[JsonValue]:
    """Return the values of a path in the items that hold one."""
    parsed = parse_path(path, relative=True)
    found: list[JsonValue] = []
    for item in items:
        value = resolve(item, parsed)
        if not isinstance(value, Missing) and value is not None:
            found.append(value)
    return found


def _in_range(values: list[JsonValue], limit: float) -> bool:
    """Tell whether all values are numbers within a limit."""
    if not values:
        return False
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        if abs(value) > limit:
            return False
    return True


def _suggest_position(
    candidate: CollectionCandidate, items: list[JsonValue]
) -> PositionPaths | None:
    """Find a geometry, or a pair of coordinates side by side."""
    for item in candidate.item_fields:
        if item.type is ScalarType.GEOMETRY:
            return PositionPaths(geometry=item.path)
    latitudes = [i.path for i in candidate.item_fields if _last_name(i.path) in _LATITUDE_NAMES]
    longitudes = [i.path for i in candidate.item_fields if _last_name(i.path) in _LONGITUDE_NAMES]
    for latitude in latitudes:
        if not _in_range(_values(items, latitude), _LATITUDE_LIMIT):
            continue
        parent = latitude.rpartition(".")[0]
        for longitude in longitudes:
            same_parent = longitude.rpartition(".")[0] == parent
            if same_parent and _in_range(_values(items, longitude), _LONGITUDE_LIMIT):
                return PositionPaths(latitude=latitude, longitude=longitude)
    return None


def _is_words(values: list[JsonValue]) -> bool:
    """Tell whether values read as words rather than codes."""
    texts = [value.strip() for value in values if isinstance(value, str)]
    return (
        bool(texts)
        and len(texts) == len(values)
        and not any(_looks_like_identifier(text) for text in texts if text)
    )


def _suggest_name(
    candidate: CollectionCandidate, items: list[JsonValue], key: str | None
) -> str | None:
    """Pick a field holding words, preferring names."""
    texts = [
        item.path
        for item in candidate.item_fields
        if item.type is ScalarType.TEXT
        and item.path != key
        and _is_words(_values(items, item.path))
    ]
    for names in (_NAME_NAMES, _TEXT_NAMES):
        for path in texts:
            if _last_name(path) in names or (words_of(path) and words_of(path)[-1] in names):
                return path
    return None


def _moment(value: JsonValue, zone: tzinfo) -> datetime | date | None:
    """Read a date or time."""
    result = read_field(
        {"v": value}, parse_path("$.v"), ValueSpec(ValueType.TIMESTAMP, naive_timezone=zone)
    )
    return result if isinstance(result, date) else None


def _date_field(
    candidate: CollectionCandidate, items: list[JsonValue], names: frozenset[str], zone: tzinfo
) -> str | None:
    """Find a text field of dates with a fitting name."""
    for item in candidate.item_fields:
        words = words_of(item.path.rpartition(".")[2])
        if item.type is not ScalarType.TEXT or not set(words) & names:
            continue
        values = _values(items, item.path)
        if values and all(_moment(value, zone) is not None for value in values):
            return item.path
    return None


def _has_future(items: list[JsonValue], path: str, now: datetime, zone: tzinfo) -> bool:
    """Tell whether any date is still to come."""
    for value in _values(items, path):
        moment = _moment(value, zone)
        if isinstance(moment, datetime):
            if moment >= now:
                return True
        elif moment is not None and moment >= now.date():
            return True
    return False


def _suggest_value(candidate: CollectionCandidate, key: str | None) -> str | None:
    """Pick a field to show as a state, preferring numbers."""
    fields = [item for item in candidate.item_fields if item.path != key]
    for wanted in (ScalarType.NUMBER, ScalarType.TEXT, ScalarType.BOOLEAN):
        for item in fields:
            if item.type is wanted:
                return item.path
    return None


def suggest_collection(
    data: JsonValue, candidate: CollectionCandidate, now: datetime, zone: tzinfo
) -> CollectionSuggestion:
    """Propose how to map a list."""
    listed = resolve(data, parse_path(candidate.path))
    items: list[JsonValue] = listed if isinstance(listed, list) else []
    key = _suggest_key(candidate)
    name = _suggest_name(candidate, items, key)
    position = _suggest_position(candidate, items)
    start = _date_field(candidate, items, _DATE_NAMES, zone)
    end = _date_field(candidate, items, _END_NAMES, zone)
    value = _suggest_value(candidate, key)
    if position is not None:
        named = name is not None and _last_name(name) in _NAME_NAMES
        kind = (
            EntityKind.DEVICE_TRACKER
            if named and position.geometry is None
            else EntityKind.GEO_LOCATION
        )
    elif start is not None and name is not None and _has_future(items, start, now, zone):
        kind = EntityKind.CALENDAR
    else:
        kind = EntityKind.SENSOR
    return CollectionSuggestion(
        kind, key, name, position, start, end, value, _example(items, name or key)
    )


def _example(items: list[JsonValue], path: str | None) -> str:
    """Show one real value."""
    if path is None:
        return ""
    values = _values(items, path)
    return str(values[0])[:60] if values else ""


def suggest_field(flat: FlatField) -> FieldSuggestion:
    """Propose how to map one field."""
    name = name_for(flat.path)
    if flat.type is ScalarType.BOOLEAN:
        return FieldSuggestion(EntityKind.BINARY_SENSOR, name, ValueType.BOOLEAN)
    if flat.type is not ScalarType.NUMBER:
        return FieldSuggestion(EntityKind.SENSOR, name, ValueType.TEXT)
    words = words_of(_named_parts(_parse(flat.path).segments)[-1])
    for word in words:
        if word in _FIELD_CLASSES:
            device_class, unit = _FIELD_CLASSES[word]
            return FieldSuggestion(
                EntityKind.SENSOR, name, ValueType.NUMBER, device_class, unit, "measurement"
            )
    return FieldSuggestion(EntityKind.SENSOR, name, ValueType.NUMBER, state_class="measurement")


@final
@dataclass(frozen=True, slots=True)
class UrlKey:
    """A credential found in a URL."""

    url: str = field(repr=False)
    name: str
    value: str = field(repr=False)


def find_url_key(url: str) -> UrlKey | None:
    """Find a query parameter that looks like a credential."""
    try:
        parsed = URL(url)
    except ValueError:
        return None
    for name, value in parsed.query.items():
        if name.casefold() in _CREDENTIAL_PARAMETERS and value:
            return UrlKey(str(parsed.without_query_params(name)), name, value)
    return None
