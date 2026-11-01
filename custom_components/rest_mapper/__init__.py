"""REST Mapper: map any JSON endpoint to entities, spending requests with care."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

# Platforms every entry uses load with the integration, away from the event loop.
from . import binary_sensor, button, sensor  # noqa: F401
from .const import CONF_STORAGE_ID
from .coordinator import EndpointCoordinator, RestMapperConfigEntry
from .entity import endpoint_device, remove_orphans
from .issues import async_remove_issues
from .kinds import KINDS
from .mappings import load_mappings
from .store import async_load, endpoint_store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_BASE_PLATFORMS: Final = frozenset({Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR})


def _platforms(entry: RestMapperConfigEntry) -> list[Platform]:
    """Return the platforms in use, so unused ones never load."""
    platforms = set(_BASE_PLATFORMS)
    for mapping in load_mappings(entry.subentries).mappings.values():
        platforms.add(Platform(KINDS[mapping.kind].platform))
        if mapping.settings.get("events") is True:
            platforms.add(Platform.EVENT)
    return sorted(platforms)


async def async_setup_entry(hass: HomeAssistant, entry: RestMapperConfigEntry) -> bool:
    """Set up from stored data. No request is sent."""
    store = endpoint_store(hass, str(entry.data[CONF_STORAGE_ID]))
    coordinator = EndpointCoordinator(hass, entry, store, await async_load(store))
    await coordinator.async_start()
    entry.runtime_data = coordinator
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **endpoint_device(coordinator)
    )
    coordinator.endpoint_device_id = device.id
    remove_orphans(hass, entry)
    coordinator.platforms = _platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: RestMapperConfigEntry) -> None:
    """Apply changed options or mappings. This sends no request."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RestMapperConfigEntry) -> bool:
    """Save at once, so a reload loses nothing."""
    coordinator = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(entry, coordinator.platforms)
    if unloaded:
        await coordinator.async_save()
        await coordinator.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: RestMapperConfigEntry) -> None:
    """Delete stored data."""
    await endpoint_store(hass, str(entry.data[CONF_STORAGE_ID])).async_remove()
    async_remove_issues(hass, entry.entry_id)
