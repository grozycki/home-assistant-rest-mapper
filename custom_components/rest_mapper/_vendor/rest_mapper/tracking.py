"""Follow list items by key across responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from .paths import JsonValue, Missing, Path, resolve

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_ITEM_LIMIT: Final = 50
MAX_ITEM_LIMIT: Final = 500
DEFAULT_REMOVAL_AFTER: Final = 2


@final
@dataclass(frozen=True, slots=True)
class ReadItems:
    """Items of one response by key, in list order."""

    items: dict[str, dict[str, JsonValue]]
    skipped: int
    duplicates: int


def read_items(data: JsonValue, list_path: Path, key_path: Path) -> ReadItems | None:
    """Read a list by key, or return `None` when no list is at the path."""
    value = resolve(data, list_path)
    if not isinstance(value, list):
        return None
    items: dict[str, dict[str, JsonValue]] = {}
    skipped = 0
    duplicates = 0
    for item in value:
        key = _item_key(item, key_path)
        if key is None or not isinstance(item, dict):
            skipped += 1
        elif key in items:
            duplicates += 1
        else:
            items[key] = item
    return ReadItems(items, skipped, duplicates)


def _item_key(item: JsonValue, key_path: Path) -> str | None:
    """Return the key of an item as text."""
    value = resolve(item, key_path)
    if isinstance(value, Missing) or value is None or isinstance(value, bool | list | dict):
        return None
    return str(value)


@final
@dataclass(frozen=True, slots=True)
class Changes:
    """What one response changed."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    ignored: int


@final
class ItemTracker:
    """The set of tracked keys, with how long each has been absent."""

    def __init__(
        self,
        limit: int = DEFAULT_ITEM_LIMIT,
        removal_after: int = DEFAULT_REMOVAL_AFTER,
    ) -> None:
        """Start with no baseline."""
        self._limit = limit
        self._removal_after = removal_after
        self._absences: dict[str, int] = {}
        self._has_baseline = False

    @property
    def tracked(self) -> tuple[str, ...]:
        """Return the tracked keys."""
        return tuple(self._absences)

    @property
    def has_baseline(self) -> bool:
        """Tell whether the first response has been seen."""
        return self._has_baseline

    def update(self, keys: tuple[str, ...]) -> Changes:
        """Apply the keys of a new response."""
        removed = self._age_absent(keys)
        for key in keys:
            if key in self._absences:
                self._absences[key] = 0
        added: list[str] = []
        ignored = 0
        for key in keys:
            if key in self._absences:
                continue
            if len(self._absences) >= self._limit:
                ignored += 1
                continue
            self._absences[key] = 0
            added.append(key)
        if not self._has_baseline:
            self._has_baseline = True
            added = []
        return Changes(tuple(added), removed, ignored)

    def _age_absent(self, keys: tuple[str, ...]) -> tuple[str, ...]:
        """Count one more absence, and drop keys that reached the limit."""
        present = set(keys)
        removed: list[str] = []
        for key in tuple(self._absences):
            if key in present:
                continue
            self._absences[key] += 1
            if self._absences[key] >= self._removal_after:
                del self._absences[key]
                removed.append(key)
        return tuple(removed)

    def to_data(self) -> dict[str, JsonValue]:
        """Return plain data for storage."""
        return {"baseline": self._has_baseline, "absences": dict(self._absences)}

    def restore(self, data: Mapping[str, JsonValue]) -> None:
        """Load plain data, ignoring anything malformed."""
        absences = data.get("absences")
        if not isinstance(absences, dict):
            return
        self._absences = {
            key: value
            for key, value in absences.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        self._has_baseline = data.get("baseline") is True
