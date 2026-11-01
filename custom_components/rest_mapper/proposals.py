"""Turn suggestions into finished mappings. Pure, so it can run in the executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from homeassistant.util import slugify

from .evaluation import read_value
from .kinds import KINDS
from .lib import (
    DEFAULT_ITEM_LIMIT,
    MAX_ITEM_LIMIT,
    CollectionCandidate,
    CollectionSuggestion,
    EntityKind,
    Failure,
    FieldSuggestion,
    FlatField,
    JsonValue,
    Position,
    ScalarType,
    ValueType,
    label_for,
    name_for,
    read_attributes,
    read_items,
    suggest_field,
)
from .mappings import mapping_data
from .values import EventData, ReadValue, ValueRead

if TYPE_CHECKING:
    from datetime import datetime, tzinfo

    from .kinds import MappingConfig
    from .texts import Texts

MAX_PROPOSED_FIELDS: Final = 8
PRESELECTED_FIELDS: Final = 3
PREVIEW_ITEMS: Final = 3
PREVIEW_NAME: Final = 32
_LIST_TEXTS: Final = {
    EntityKind.DEVICE_TRACKER: "list_places",
    EntityKind.GEO_LOCATION: "list_positions",
    EntityKind.CALENDAR: "list_calendar",
    EntityKind.SENSOR: "list_sensors",
    EntityKind.BINARY_SENSOR: "list_sensors",
}


@final
@dataclass(frozen=True, slots=True)
class Proposal:
    """One thing the found screen offers."""

    identifier: str
    label: str
    subentry_type: str
    title: str
    data: dict[str, JsonValue]
    preselected: bool


def roles_for(kind: str, suggestion: CollectionSuggestion) -> dict[str, str]:
    """Pick the paths a kind needs from a suggestion."""
    roles: dict[str, str] = {}
    if suggestion.name:
        roles["summary" if kind == EntityKind.CALENDAR else "name"] = suggestion.name
    position = suggestion.position
    if kind == EntityKind.CALENDAR:
        roles.update({"start": suggestion.start or "", "end": suggestion.end or ""})
    elif kind in (EntityKind.DEVICE_TRACKER, EntityKind.GEO_LOCATION):
        if position is not None and position.geometry:
            roles["geometry"] = position.geometry
        elif position is not None:
            roles.update(
                {"latitude": position.latitude or "", "longitude": position.longitude or ""}
            )
    elif suggestion.value:
        roles["value"] = suggestion.value
    return {name: path for name, path in roles.items() if path}


def item_limit_for(length: int) -> int:
    """Leave room for a list to grow."""
    return min(max(DEFAULT_ITEM_LIMIT, length * 2), MAX_ITEM_LIMIT)


def value_type_of(candidate: CollectionCandidate, path: str | None) -> ValueType:
    """Read a field in a way that fits what it holds."""
    for item in candidate.item_fields:
        if item.path == path and item.type is ScalarType.NUMBER:
            return ValueType.NUMBER
    return ValueType.TEXT


def collection_settings(
    kind: str, candidate: CollectionCandidate, suggestion: CollectionSuggestion, title: str
) -> dict[str, JsonValue]:
    """Build settings for a proposed collection."""
    settings: dict[str, JsonValue] = {
        "events": kind != EntityKind.CALENDAR,
        "item_limit": item_limit_for(candidate.length),
        "removal_after": 1 if kind == EntityKind.GEO_LOCATION else 2,
        "source": slugify(title),
    }
    if kind == EntityKind.SENSOR:
        settings["value_type"] = value_type_of(candidate, suggestion.value).value
    if kind == EntityKind.CALENDAR:
        settings["naive_local"] = True
    return settings


def collection_title(path: str, fallback: str) -> str:
    """Name a list after its path, or the endpoint for a list at the root."""
    return fallback if path == "$" else name_for(path)


def _list_proposal(
    candidate: CollectionCandidate, suggestion: CollectionSuggestion, fallback: str, texts: Texts
) -> Proposal | None:
    """Offer a list, if its items can be told apart."""
    if suggestion.key is None:
        return None
    kind = suggestion.kind.value
    roles = roles_for(kind, suggestion)
    if not all(role.name in roles for role in KINDS[kind].roles if role.required):
        return None
    title = collection_title(candidate.path, fallback)
    data = mapping_data(
        kind,
        roles,
        collection_settings(kind, candidate, suggestion, title),
        {},
        list_path=candidate.path,
        key_path=suggestion.key,
    )
    label = texts(
        _LIST_TEXTS[suggestion.kind],
        count=candidate.length,
        title=title,
        example=suggestion.example or suggestion.key,
    )
    return Proposal(f"list:{candidate.path}", label, "collection", title, data, True)


def field_data(suggestion: FieldSuggestion, path: str) -> dict[str, JsonValue]:
    """Build a mapping for one field."""
    settings: dict[str, JsonValue] = {}
    if suggestion.kind is EntityKind.SENSOR:
        settings["value_type"] = suggestion.value_type.value
        for key, value in (
            ("device_class", suggestion.device_class),
            ("unit_of_measurement", suggestion.unit),
            ("state_class", suggestion.state_class),
        ):
            if value is not None:
                settings[key] = value
    return mapping_data(suggestion.kind.value, {"value": path}, settings, {})


def _field_proposals(fields: tuple[FlatField, ...], *, preselect: bool) -> list[Proposal]:
    """Offer top-level fields. List items are offered through their list."""
    usable = [
        item
        for item in fields
        if "[" not in item.path and item.type not in (ScalarType.NULL, ScalarType.GEOMETRY)
    ]
    proposals: list[Proposal] = []
    for index, item in enumerate(usable[:MAX_PROPOSED_FIELDS]):
        suggestion = suggest_field(item)
        proposals.append(
            Proposal(
                f"field:{item.path}",
                f"{label_for(item.path)}: {item.example}",
                suggestion.kind.value,
                suggestion.name,
                field_data(suggestion, item.path),
                preselect and index < PRESELECTED_FIELDS,
            )
        )
    return proposals


def build_proposals(
    lists: list[tuple[CollectionCandidate, CollectionSuggestion]],
    fields: tuple[FlatField, ...],
    fallback: str,
    texts: Texts,
) -> list[Proposal]:
    """List everything the found screen offers."""
    proposals = [
        proposal
        for candidate, suggestion in lists
        if (proposal := _list_proposal(candidate, suggestion, fallback, texts)) is not None
    ]
    return [*proposals, *_field_proposals(fields, preselect=not proposals)]


def attribute_notes(document: JsonValue, mapping: MappingConfig, texts: Texts) -> list[str]:
    """List attributes that cannot be read from a document."""
    reads = read_attributes(document, mapping.attributes)
    missing = set(mapping.attributes) - set(reads.values) - set(reads.too_large)
    notes = [texts("attribute_missing", name=name) for name in sorted(missing)]
    notes.extend(texts("attribute_too_large", name=name) for name in reads.too_large)
    return notes


@final
@dataclass(frozen=True, slots=True)
class Check:
    """What a mapping reads from stored data."""

    summary: str
    failure: Failure | None = None
    empty: bool = False


def _describe(value: ReadValue, mapping: MappingConfig) -> str:
    """Write a read value."""
    if isinstance(value, Position):
        return f"{value.latitude}, {value.longitude}"
    if isinstance(value, EventData):
        return value.summary
    unit = mapping.settings.get("unit_of_measurement")
    return f"{value} {unit}" if isinstance(unit, str) and unit else str(value)


def check_collection(
    body: JsonValue, mapping: MappingConfig, now: datetime, zone: tzinfo, texts: Texts
) -> Check:
    """Read a collection from stored data, as a summary and a verdict."""
    if mapping.list_path is None or mapping.key_path is None:
        return Check("", Failure.MISSING)
    items = read_items(body, mapping.list_path, mapping.key_path)
    if items is None:
        return Check(texts("no_list"), Failure.MISSING)
    reads = {
        key: read_value(item, mapping, ValueRead(), now, zone) for key, item in items.items.items()
    }
    failed = [read for read in reads.values() if read.failure is not None]
    if not reads and items.skipped == 0:
        return Check(texts("summary", count=0, skipped=0), empty=True)
    lines = [texts("summary", count=len(reads), skipped=items.skipped + len(failed))]
    for key, read in list(reads.items())[:PREVIEW_ITEMS]:
        if read.failure is None:
            lines.append(f"{(read.name or key)[:PREVIEW_NAME]}: {_describe(read.value, mapping)}")
    for item in list(items.items.values())[:1]:
        lines.extend(attribute_notes(item, mapping, texts))
    summary = "\n\n".join(lines)
    if not reads:
        return Check(summary, Failure.TYPE_MISMATCH)
    if len(failed) == len(reads):
        first = failed[0].failure
        return Check(summary, first if first is not None else Failure.TYPE_MISMATCH)
    return Check(summary)


def check_single(
    body: JsonValue, mapping: MappingConfig, now: datetime, zone: tzinfo, texts: Texts
) -> Check:
    """Read one mapping from stored data, as a summary and a verdict."""
    read = read_value(body, mapping, ValueRead(), now, zone)
    notes = attribute_notes(body, mapping, texts)
    if read.failure is not None:
        return Check("\n\n".join((texts(f"reason_{read.failure.value}"), *notes)), read.failure)
    return Check("\n\n".join((_describe(read.value, mapping), *notes)))
