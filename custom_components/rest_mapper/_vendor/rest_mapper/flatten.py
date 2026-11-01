"""List what a response offers, for choosing paths."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

from .paths import MISSING, JsonValue, Segment, format_path, parse_path, resolve

MAX_DEPTH: Final = 12
MAX_ENTRIES: Final = 1000
MAX_LISTED_ITEMS: Final = 20
MAX_EXAMPLE_LENGTH: Final = 60
MAX_SAMPLED_ITEMS: Final = 50


class ScalarType(StrEnum):
    """Type of a flattened value."""

    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    NULL = "null"


@final
@dataclass(frozen=True, slots=True)
class FlatField:
    """One selectable value."""

    path: str
    type: ScalarType
    example: str


@final
@dataclass(frozen=True, slots=True)
class CollectionCandidate:
    """A list of objects that could become a collection."""

    path: str
    length: int
    item_fields: tuple[FlatField, ...]
    likely_keys: tuple[str, ...]


@final
@dataclass(frozen=True, slots=True)
class Flattened:
    """Everything found in a response."""

    fields: tuple[FlatField, ...]
    collections: tuple[CollectionCandidate, ...]
    truncated: bool


@final
class _Walker:
    """Collect fields and candidates, stopping at the limits."""

    def __init__(self, *, relative: bool) -> None:
        self.relative = relative
        self.fields: dict[str, FlatField] = {}
        self.collections: list[CollectionCandidate] = []
        self.truncated = False

    def walk(self, value: JsonValue, segments: tuple[Segment, ...]) -> None:
        """Visit one value."""
        if len(self.fields) >= MAX_ENTRIES or len(segments) > MAX_DEPTH:
            self.truncated = True
        elif isinstance(value, dict):
            for key, child in value.items():
                self.walk(child, (*segments, key))
        elif isinstance(value, list):
            self._walk_list(value, segments)
        else:
            path = format_path(segments, relative=self.relative)
            self.fields[path] = FlatField(path, _scalar_type(value), _example(value))

    def _walk_list(self, items: list[JsonValue], segments: tuple[Segment, ...]) -> None:
        """Visit the first items of a list, and note it when it holds objects."""
        if not self.relative and items and all(isinstance(item, dict) for item in items):
            self.collections.append(_candidate(items, segments))
        for index, item in enumerate(items[:MAX_LISTED_ITEMS]):
            self.walk(item, (*segments, index))
        if len(items) > MAX_LISTED_ITEMS:
            self.truncated = True


def flatten(data: JsonValue) -> Flattened:
    """List scalar paths and collection candidates of a response."""
    walker = _Walker(relative=False)
    walker.walk(data, ())
    return Flattened(tuple(walker.fields.values()), tuple(walker.collections), walker.truncated)


def _candidate(items: list[JsonValue], segments: tuple[Segment, ...]) -> CollectionCandidate:
    """Describe a list of objects."""
    walker = _Walker(relative=True)
    for item in items[:MAX_SAMPLED_ITEMS]:
        walker.walk(item, ())
    item_fields = tuple(walker.fields.values())
    likely_keys = tuple(field.path for field in item_fields if _is_likely_key(items, field.path))
    return CollectionCandidate(format_path(segments), len(items), item_fields, likely_keys)


def _is_likely_key(items: list[JsonValue], text: str) -> bool:
    """Tell whether a relative path holds a unique scalar in every item."""
    path = parse_path(text, relative=True)
    seen: set[str] = set()
    for item in items:
        value = resolve(item, path)
        if value is MISSING or value is None or isinstance(value, bool | list | dict):
            return False
        key = str(value)
        if key in seen:
            return False
        seen.add(key)
    return True


def _scalar_type(value: JsonValue) -> ScalarType:
    """Classify a scalar."""
    if value is None:
        return ScalarType.NULL
    if isinstance(value, bool):
        return ScalarType.BOOLEAN
    if isinstance(value, int | float):
        return ScalarType.NUMBER
    return ScalarType.TEXT


def _example(value: JsonValue) -> str:
    """Shorten a scalar for showing."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if len(text) > MAX_EXAMPLE_LENGTH:
        return f"{text[: MAX_EXAMPLE_LENGTH - 1]}…"
    return text
