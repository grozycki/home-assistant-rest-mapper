"""What an endpoint keeps across restarts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, final

from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .lib import JsonValue, PolicyState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

KEY_BODY: Final = "body"
KEY_POLICY: Final = "policy"
KEY_READS: Final = "reads"
KEY_COLLECTIONS: Final = "collections"
KEY_FINGERPRINTS: Final = "fingerprints"
KEY_CONFIRMED: Final = "confirmed_at"


@final
@dataclass(slots=True)
class StoredEndpoint:
    """The stored form of an endpoint."""

    body: JsonValue = None
    has_body: bool = False
    policy: PolicyState = field(default_factory=PolicyState)
    reads: dict[str, JsonValue] = field(default_factory=dict)
    collections: dict[str, JsonValue] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    confirmed_at: datetime | None = None

    def to_data(self) -> dict[str, JsonValue]:
        """Return plain data."""
        return {
            KEY_BODY: [self.body] if self.has_body else [],
            KEY_POLICY: self.policy.to_data(),
            KEY_READS: self.reads,
            KEY_COLLECTIONS: self.collections,
            KEY_FINGERPRINTS: dict(self.fingerprints),
            KEY_CONFIRMED: self.confirmed_at.isoformat() if self.confirmed_at else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, JsonValue] | None) -> StoredEndpoint:
        """Read plain data, or start empty."""
        if data is None:
            return cls()
        body = data.get(KEY_BODY)
        policy = data.get(KEY_POLICY)
        reads = data.get(KEY_READS)
        collections = data.get(KEY_COLLECTIONS)
        fingerprints = data.get(KEY_FINGERPRINTS)
        confirmed = data.get(KEY_CONFIRMED)
        has_body = isinstance(body, list) and len(body) == 1
        return cls(
            body=body[0] if isinstance(body, list) and has_body else None,
            has_body=has_body,
            policy=PolicyState.from_data(policy if isinstance(policy, dict) else {}),
            reads=reads if isinstance(reads, dict) else {},
            collections=collections if isinstance(collections, dict) else {},
            fingerprints=(
                {key: value for key, value in fingerprints.items() if isinstance(value, str)}
                if isinstance(fingerprints, dict)
                else {}
            ),
            confirmed_at=(
                datetime.fromisoformat(confirmed) if isinstance(confirmed, str) else None
            ),
        )


def endpoint_store(hass: HomeAssistant, storage_id: str) -> Store[dict[str, JsonValue]]:
    """Return the store of an endpoint."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{storage_id}", atomic_writes=True)


async def async_load(store: Store[dict[str, JsonValue]]) -> StoredEndpoint:
    """Load what is stored."""
    return StoredEndpoint.from_data(await store.async_load())
