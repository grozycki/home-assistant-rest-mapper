"""Diagnostics. Only allowed fields are copied, so no secret can slip in."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_AUTH_NAME,
    CONF_AUTH_TYPE,
    CONF_BUDGET_LIMIT,
    CONF_BUDGET_PERIOD,
    CONF_INTERVAL,
    CONF_MAX_KIB,
    CONF_MODE,
    CONF_RESERVE,
    CONF_RETENTION,
    CONF_TIMEOUT,
    CONF_URL,
)
from .lib import flatten, redact_url

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EndpointCoordinator, RestMapperConfigEntry


_OPTIONS = (
    CONF_MODE,
    CONF_INTERVAL,
    CONF_BUDGET_LIMIT,
    CONF_BUDGET_PERIOD,
    CONF_RESERVE,
    CONF_RETENTION,
    CONF_MAX_KIB,
    CONF_TIMEOUT,
)


def _policy(coordinator: EndpointCoordinator) -> dict[str, object]:
    """Describe the policy, leaving out validators."""
    state = coordinator.policy.state
    return {
        "status": coordinator.status().value,
        "used": state.used,
        "failures": state.failures,
        "rate_limit_streak": state.rate_limit_streak,
        "auth_stopped": state.auth_stopped,
        "last_request": state.last_request,
        "backoff_until": state.backoff_until,
        "rate_limited_until": state.rate_limited_until,
        "next_request": coordinator.policy.next_request_at(),
        "has_validators": coordinator.policy.validators() is not None,
    }


def _mappings(coordinator: EndpointCoordinator) -> list[dict[str, object]]:
    """Describe mappings by path and result, with no values."""
    data = coordinator.data
    results: list[dict[str, object]] = []
    for mapping in coordinator.mappings.values():
        read = data.reads.get(mapping.subentry_id)
        collection = data.collections.get(mapping.subentry_id)
        results.append(
            {
                "kind": mapping.kind,
                "shape": mapping.shape.value,
                "roles": {name: str(path) for name, path in mapping.roles.items()},
                "attributes": {name: str(path) for name, path in mapping.attributes.items()},
                "list_path": str(mapping.list_path) if mapping.list_path else None,
                "failure": read.failure.value if read is not None and read.failure else None,
                "failures": read.failures if read is not None else None,
                "tracked_items": len(collection.tracker.tracked) if collection else None,
                "skipped": collection.skipped if collection else None,
                "ignored": collection.ignored if collection else None,
            }
        )
    return results


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RestMapperConfigEntry
) -> dict[str, object]:
    """Return settings, status and mapping results."""
    coordinator = entry.runtime_data
    found = (
        await hass.async_add_executor_job(flatten, coordinator.stored.body)
        if coordinator.stored.has_body
        else None
    )
    auth_type = entry.data.get(CONF_AUTH_TYPE)
    return {
        "url": redact_url(str(entry.data[CONF_URL])),
        "auth_type": auth_type,
        "auth_name": entry.data.get(CONF_AUTH_NAME) if auth_type == "header" else None,
        "options": {key: entry.options.get(key) for key in _OPTIONS},
        "policy": _policy(coordinator),
        "mappings": _mappings(coordinator),
        "skipped_mappings": len(coordinator.skipped),
        "response_paths": (
            {item.path: item.type.value for item in found.fields} if found is not None else {}
        ),
    }
