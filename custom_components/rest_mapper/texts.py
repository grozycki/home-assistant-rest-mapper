"""Sentences composed at run time, as flow labels cannot be translated by key."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, final

from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_PREFIX: Final = f"component.{DOMAIN}.selector.text.options."


@final
class Texts:
    """Translated sentences by key."""

    def __init__(self, strings: Mapping[str, str]) -> None:
        """Keep the strings."""
        self._strings = strings

    def __call__(self, key: str, **values: object) -> str:
        """Return a sentence with its values filled in."""
        return self._strings.get(f"{_PREFIX}{key}", key).format(**values)


@final
@dataclass(frozen=True, slots=True)
class ClassLabels:
    """Names of device and state classes, from Home Assistant's own translations."""

    sensor: Mapping[str, str] = field(default_factory=dict)
    binary_sensor: Mapping[str, str] = field(default_factory=dict)
    state_class: Mapping[str, str] = field(default_factory=dict)


def _names(strings: Mapping[str, str], domain: str) -> dict[str, str]:
    """Read class names of a domain."""
    prefix = f"component.{domain}.entity_component."
    names: dict[str, str] = {}
    for key, value in strings.items():
        name, _, rest = key.removeprefix(prefix).partition(".")
        if key.startswith(prefix) and rest == "name" and name != "_":
            names[name] = value
    return names


async def async_load_class_labels(hass: HomeAssistant) -> ClassLabels:
    """Load class names in Home Assistant's language."""
    strings = await async_get_translations(
        hass, hass.config.language, "entity_component", {"sensor", "binary_sensor"}
    )
    prefix = "component.sensor.entity_component._.state_attributes.state_class.state."
    states = {
        key.removeprefix(prefix): value for key, value in strings.items() if key.startswith(prefix)
    }
    return ClassLabels(_names(strings, "sensor"), _names(strings, "binary_sensor"), states)


async def async_load_texts(hass: HomeAssistant) -> Texts:
    """Load sentences in Home Assistant's language."""
    return Texts(await async_get_translations(hass, hass.config.language, "selector", {DOMAIN}))
