"""Shared behaviour of all mapped entities."""

from __future__ import annotations

import base64
import hashlib
import html
from collections.abc import Callable
from typing import TYPE_CHECKING, override

from homeassistant.const import MATCH_ALL
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_LAST_CONFIRMED,
    CONFIRMATION_WRITE_INTERVAL,
    DOMAIN,
    ICON_PREFIX,
    MAX_KEY_LENGTH,
    MAX_NAME_LENGTH,
    SHORT_KEY_LENGTH,
)
from .coordinator import EndpointCoordinator, RestMapperConfigEntry
from .evaluation import is_collection
from .kinds import KINDS, MappingConfig, Shape
from .values import ValueRead

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


def endpoint_device(coordinator: EndpointCoordinator) -> DeviceInfo:
    """Describe the endpoint."""
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="REST Mapper",
        model="Endpoint",
        entry_type=DeviceEntryType.SERVICE,
    )


def collection_device(coordinator: EndpointCoordinator, mapping: MappingConfig) -> DeviceInfo:
    """Describe a collection."""
    return DeviceInfo(
        identifiers={(DOMAIN, mapping.subentry_id)},
        name=mapping.title,
        manufacturer="REST Mapper",
        model="Collection",
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=coordinator.endpoint_device_id,
    )


def picture_for(text: str) -> str:
    """Draw a short text, such as an emoji, as an image."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        '<text x="20" y="29" font-size="28" text-anchor="middle">'
        f"{html.escape(text)}</text></svg>"
    )
    return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"


def item_unique_id(subentry_id: str, key: str) -> str:
    """Build an item's unique ID, hashing long keys."""
    if len(key) > MAX_KEY_LENGTH:
        key = hashlib.sha256(key.encode()).hexdigest()
    return f"{subentry_id}_item_{key}"


class MappedEntity(CoordinatorEntity[EndpointCoordinator]):
    """Shows a read, keeps it through failures, and hides nothing about its age."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({MATCH_ALL})
    _written_confirmation: datetime | None = None
    _written_content: object = None

    def __init__(
        self,
        coordinator: EndpointCoordinator,
        mapping: MappingConfig,
        key: str | None = None,
    ) -> None:
        """Bind to a mapping, and to an item's key in a collection."""
        super().__init__(coordinator)
        self.mapping = mapping
        self.key = key

    def _setup_identity(self) -> None:
        """Set the unique ID, name and device."""
        mapping = self.mapping
        if self.key is None:
            self._attr_unique_id = mapping.subentry_id
            self._attr_name = mapping.title
            self._attr_device_info = endpoint_device(self.coordinator)
            return
        self._attr_unique_id = item_unique_id(mapping.subentry_id, self.key)
        self._attr_device_info = collection_device(self.coordinator, mapping)

    @property
    def current(self) -> ValueRead:
        """Return the read this entity shows."""
        data = self.coordinator.data
        if self.key is None:
            return data.reads.get(self.mapping.subentry_id, ValueRead())
        collection = data.collections.get(self.mapping.subentry_id)
        if collection is None:
            return ValueRead()
        return collection.items.get(self.key, ValueRead())

    @property
    @override
    def available(self) -> bool:
        """Stay available through failures, up to the retention limit."""
        return not self.coordinator.expired

    @property
    @override
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return mapped attributes and the confirmation time."""
        read = self.current
        attributes: dict[str, object] = dict(read.attributes)
        if read.confirmed_at is not None:
            attributes[ATTR_LAST_CONFIRMED] = read.confirmed_at.isoformat()
        return attributes

    @property
    @override
    def icon(self) -> str | None:
        """Return an icon chosen by a value."""
        icon = self.current.icon
        return icon if icon is not None and icon.startswith(ICON_PREFIX) else None

    @property
    @override
    def entity_picture(self) -> str | None:
        """Draw anything but an icon name, as the default Map shows only pictures."""
        icon = self.current.icon
        if icon is None or icon.startswith(ICON_PREFIX):
            return None
        return picture_for(icon)

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Write when something changed, and rarely when only the age did."""
        read = self.current
        content = (read.value, read.attributes, read.name, read.icon, self.available)
        confirmed = read.confirmed_at
        written = self._written_confirmation
        age_is_due = confirmed is not None and (
            written is None or confirmed - written >= CONFIRMATION_WRITE_INTERVAL
        )
        if content != self._written_content or age_is_due:
            self._written_content = content
            self._written_confirmation = confirmed
            self.async_write_ha_state()


class ItemName(MappedEntity):
    """Names an item after its name path, or its key."""

    @property
    @override
    def name(self) -> str | None:
        """Return the item's name."""
        if self.key is None:
            return self.mapping.title
        if name := self.current.name:
            return name
        if len(self.key) > MAX_NAME_LENGTH:
            digest = hashlib.sha256(self.key.encode()).hexdigest()
            return f"{self.mapping.title} {digest[:SHORT_KEY_LENGTH]}"
        return self.key


type ItemFactory = Callable[[EndpointCoordinator, MappingConfig, str], Entity]


def remove_orphans(hass: HomeAssistant, entry: RestMapperConfigEntry) -> None:
    """Remove entities that no mapping stands for any more.

    A device can belong to only one subentry, and all single mappings share the endpoint's
    device, so their entities are tied to the entry and cleaned up here. So are collection
    entities left behind by a change of kind, which no platform would look at again.
    """
    registry = er.async_get(hass)
    mappings = entry.runtime_data.mappings
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.config_subentry_id is None:
            orphaned = (
                not entity.unique_id.startswith(entry.entry_id)
                and entity.unique_id not in entry.subentries
            )
        elif entity.config_subentry_id in mappings:
            mapping = mappings[entity.config_subentry_id]
            orphaned = _left_behind(entity.domain, entity.unique_id, mapping)
        else:
            orphaned = False
        if orphaned:
            registry.async_remove(entity.entity_id)


def _left_behind(domain: str, unique_id: str, mapping: MappingConfig) -> bool:
    """Tell whether a collection's entity no longer fits its mapping."""
    subentry_id = mapping.subentry_id
    platform = KINDS[mapping.kind].platform
    if unique_id.startswith(f"{subentry_id}_item_") or unique_id == subentry_id:
        return domain != platform
    if unique_id == f"{subentry_id}_new_item":
        return mapping.settings.get("events") is not True
    return False


def setup_singles(
    entry: RestMapperConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    factory: Callable[[EndpointCoordinator, MappingConfig], Entity],
) -> None:
    """Add one entity per single mapping of a kind."""
    coordinator = entry.runtime_data
    for mapping in coordinator.mappings.values():
        if mapping.kind == kind and mapping.shape is Shape.SINGLE:
            add_entities([factory(coordinator, mapping)])


class ItemManager:
    """Adds and removes a collection's entities as its items change."""

    def __init__(
        self,
        entry: RestMapperConfigEntry,
        add_entities: AddConfigEntryEntitiesCallback,
        domain: str,
        mapping: MappingConfig,
        factory: ItemFactory,
        wanted: Callable[[EndpointCoordinator, MappingConfig], set[str]],
    ) -> None:
        """Keep what is needed to follow one collection."""
        self._entry = entry
        self._add = add_entities
        self._domain = domain
        self._mapping = mapping
        self._factory = factory
        self._wanted = wanted
        self._entities: dict[str, Entity] = {}

    def start(self) -> None:
        """Clean up, add current items and follow changes."""
        self._remove_orphans()
        coordinator = self._entry.runtime_data
        self._entry.async_on_unload(coordinator.async_add_listener(self.update))
        self.update()

    def _registry_ids(self) -> dict[str, str]:
        """Return this collection's registered entities by unique ID."""
        registry = er.async_get(self._entry.runtime_data.hass)
        return {
            entity.unique_id: entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, self._entry.entry_id)
            if entity.domain == self._domain
            and entity.config_subentry_id == self._mapping.subentry_id
            and entity.unique_id.startswith(f"{self._mapping.subentry_id}_item_")
        }

    def _remove_orphans(self) -> None:
        """Drop registry entries of items no longer tracked."""
        coordinator = self._entry.runtime_data
        wanted = {
            item_unique_id(self._mapping.subentry_id, key)
            for key in self._wanted(coordinator, self._mapping)
        }
        registry = er.async_get(coordinator.hass)
        for unique_id, entity_id in self._registry_ids().items():
            if unique_id not in wanted:
                registry.async_remove(entity_id)

    @callback
    def update(self) -> None:
        """Bring entities in line with the wanted keys."""
        coordinator = self._entry.runtime_data
        wanted = self._wanted(coordinator, self._mapping)
        for key in tuple(self._entities):
            if key not in wanted:
                self._remove(key, self._entities.pop(key))
        new = {
            key: self._factory(coordinator, self._mapping, key)
            for key in wanted
            if key not in self._entities
        }
        if new:
            self._entities.update(new)
            self._add(list(new.values()), config_subentry_id=self._mapping.subentry_id)

    def _remove(self, key: str, entity: Entity) -> None:
        """Remove an entity, with its registry entry if it has one.

        A disabled entity was never added, so only the registry knows about it.
        """
        hass = self._entry.runtime_data.hass
        registry = er.async_get(hass)
        unique_id = item_unique_id(self._mapping.subentry_id, key)
        entity_id = registry.async_get_entity_id(self._domain, DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)
        else:
            _ = self._entry.async_create_task(hass, entity.async_remove(force_remove=True))


def tracked_keys(coordinator: EndpointCoordinator, mapping: MappingConfig) -> set[str]:
    """Return a collection's tracked keys with a read."""
    collection = coordinator.data.collections.get(mapping.subentry_id)
    return set(collection.items) if collection is not None else set()


def setup_items(
    entry: RestMapperConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    domain: str,
    factory: ItemFactory,
    wanted: Callable[[EndpointCoordinator, MappingConfig], set[str]] = tracked_keys,
) -> None:
    """Follow every collection of a kind."""
    for mapping in entry.runtime_data.mappings.values():
        if mapping.kind == kind and is_collection(mapping) and mapping.shape is Shape.ITEM:
            ItemManager(entry, add_entities, domain, mapping, factory, wanted).start()
