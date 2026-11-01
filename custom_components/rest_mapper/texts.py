"""Sentences composed at run time, as flow labels cannot be translated by key."""

from __future__ import annotations

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


async def async_load_texts(hass: HomeAssistant) -> Texts:
    """Load sentences in Home Assistant's language."""
    return Texts(await async_get_translations(hass, hass.config.language, "selector", {DOMAIN}))
