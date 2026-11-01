"""Turn stored subentry data into mappings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from .const import COLLECTION, DOMAIN, MAPPING_VERSION
from .kinds import KINDS, KindDefinition, MappingConfig, Shape
from .lib import JsonValue, Path, PathError, parse_path

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry


@final
@dataclass(frozen=True, slots=True)
class LoadedMappings:
    """Mappings that can run, and those skipped."""

    mappings: dict[str, MappingConfig]
    skipped: dict[str, str]


def _text_map(value: object) -> dict[str, str]:
    """Read a mapping of text to text."""
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str) and item}


def _json(value: object) -> JsonValue:
    """Keep only JSON values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return None


def parse_mapping(
    subentry_id: str, title: str, subentry_type: str, data: Mapping[str, object]
) -> MappingConfig | None:
    """Read one subentry. Returns `None` for a kind or path it cannot use."""
    kind = data.get("kind")
    definition = KINDS.get(kind) if isinstance(kind, str) else None
    if definition is None or not definition.available():
        return None
    collection = subentry_type == COLLECTION
    shape = _shape(definition, collection=collection)
    try:
        roles = {
            name: parse_path(text, relative=collection)
            for name, text in _text_map(data.get("roles")).items()
        }
        attributes = {
            name: parse_path(text, relative=collection)
            for name, text in _text_map(data.get("attributes")).items()
        }
        list_path = _optional_path(data.get("list_path"), relative=False)
        key_path = _optional_path(data.get("key_path"), relative=True)
    except PathError:
        return None
    settings = _json(data.get("settings"))
    return MappingConfig(
        subentry_id,
        title,
        definition.name,
        shape,
        roles,
        settings if isinstance(settings, dict) else {},
        attributes,
        _fingerprint(data),
        list_path,
        key_path,
    )


def _shape(definition: KindDefinition, *, collection: bool) -> Shape:
    """Return what a mapping stands for."""
    if not collection:
        return Shape.SINGLE
    return Shape.WHOLE_LIST if Shape.WHOLE_LIST in definition.shapes else Shape.ITEM


def _optional_path(value: object, *, relative: bool) -> Path | None:
    """Parse a path that may be absent."""
    return parse_path(value, relative=relative) if isinstance(value, str) else None


def _fingerprint(data: Mapping[str, object]) -> str:
    """Identify the settings a stored read came from."""
    text = json.dumps(_json(data), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_mappings(subentries: Mapping[str, ConfigSubentry]) -> LoadedMappings:
    """Read all subentries of an entry."""
    mappings: dict[str, MappingConfig] = {}
    skipped: dict[str, str] = {}
    for subentry_id, subentry in subentries.items():
        parsed = _from_subentry(subentry)
        if parsed is None:
            skipped[subentry_id] = subentry.title
        else:
            mappings[subentry_id] = parsed
    return LoadedMappings(mappings, skipped)


def _from_subentry(subentry: ConfigSubentry) -> MappingConfig | None:
    """Read one subentry."""
    return parse_mapping(
        subentry.subentry_id, subentry.title, subentry.subentry_type, subentry.data
    )


def mapping_data(
    kind: str,
    roles: Mapping[str, str],
    settings: Mapping[str, JsonValue],
    attributes: Mapping[str, str],
    *,
    list_path: str | None = None,
    key_path: str | None = None,
) -> dict[str, JsonValue]:
    """Build stored subentry data."""
    data: dict[str, JsonValue] = {
        "version": MAPPING_VERSION,
        "kind": kind,
        "roles": dict(roles),
        "settings": dict(settings),
        "attributes": dict(attributes),
    }
    if list_path is not None:
        data["list_path"] = list_path
    if key_path is not None:
        data["key_path"] = key_path
    return data


def source_for(mapping: MappingConfig) -> str:
    """Return the value for a map card's `geo_location_sources`."""
    slug = mapping.settings.get("source")
    return f"{DOMAIN}_{slug if isinstance(slug, str) else mapping.subentry_id.lower()}"
