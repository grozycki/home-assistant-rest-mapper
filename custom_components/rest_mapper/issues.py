"""Repair issues, raised and cleared after each response."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, REPAIR_AFTER_FAILURES
from .evaluation import is_collection

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EndpointCoordinator, EndpointData
    from .kinds import MappingConfig

_BROKEN_SHARE: Final = 0.5


def issue_id(entry_id: str, name: str) -> str:
    """Build an issue's ID."""
    return f"{entry_id}_{name}"


def _set_issue(
    coordinator: EndpointCoordinator,
    name: str,
    translation_key: str,
    active: bool,
    placeholders: dict[str, str],
) -> None:
    """Raise or clear one issue."""
    hass = coordinator.hass
    identifier = issue_id(coordinator.config_entry.entry_id, name)
    if not active:
        ir.async_delete_issue(hass, DOMAIN, identifier)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        identifier,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders=placeholders,
    )


def _update_field(
    coordinator: EndpointCoordinator, data: EndpointData, mapping: MappingConfig
) -> None:
    """Report a mapping that cannot be read."""
    read = data.reads.get(mapping.subentry_id)
    failures = read.failures if read is not None else 0
    roles = ", ".join(str(path) for path in mapping.roles.values())
    _set_issue(
        coordinator,
        f"{mapping.subentry_id}_broken",
        "broken_mapping",
        failures >= REPAIR_AFTER_FAILURES,
        {
            "mapping": mapping.title,
            "path": roles,
            "reason": read.failure.value if read is not None and read.failure else "",
        },
    )


def _update_collection(
    coordinator: EndpointCoordinator, data: EndpointData, mapping: MappingConfig
) -> None:
    """Report a collection's problems."""
    collection = data.collections.get(mapping.subentry_id)
    if collection is None:
        return
    subentry_id = mapping.subentry_id
    title = {"mapping": mapping.title}
    _set_issue(
        coordinator,
        f"{subentry_id}_list",
        "collection_list_missing",
        collection.list_failures >= REPAIR_AFTER_FAILURES,
        {**title, "path": str(mapping.list_path)},
    )
    _set_issue(
        coordinator,
        f"{subentry_id}_limit",
        "collection_limit",
        collection.ignored > 0,
        {
            **title,
            "limit": str(mapping.settings.get("item_limit", "")),
            "ignored": str(collection.ignored),
        },
    )
    broken = collection.skipped + collection.unreadable
    _set_issue(
        coordinator,
        f"{subentry_id}_keys",
        "collection_unreadable",
        collection.listed > 0 and broken / collection.listed > _BROKEN_SHARE,
        {**title, "path": str(mapping.key_path), "count": str(broken)},
    )


def async_update_issues(coordinator: EndpointCoordinator, data: EndpointData | None = None) -> None:
    """Bring all issues of an endpoint up to date."""
    current = data if data is not None else coordinator.data
    for mapping in coordinator.mappings.values():
        if is_collection(mapping):
            _update_collection(coordinator, current, mapping)
        else:
            _update_field(coordinator, current, mapping)
    for subentry_id, title in coordinator.skipped.items():
        _set_issue(coordinator, f"{subentry_id}_unknown", "unknown_kind", True, {"mapping": title})
    _set_issue(
        coordinator,
        "rate_limited",
        "rate_limited",
        coordinator.policy.state.rate_limit_streak >= REPAIR_AFTER_FAILURES,
        {"endpoint": coordinator.name},
    )


def async_remove_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove an endpoint's issues."""
    prefix = f"{entry_id}_"
    for domain, identifier in tuple(ir.async_get(hass).issues):
        if domain == DOMAIN and identifier.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, identifier)
