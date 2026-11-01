"""Fixtures: Home Assistant with a fake `fetch`, as the library has its own HTTP tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import probatio as vol
import pytest
from custom_components.rest_mapper.const import (
    CONF_AUTH_TYPE,
    CONF_INTERVAL,
    CONF_MODE,
    CONF_RETENTION,
    CONF_STORAGE_ID,
    CONF_URL,
    DOMAIN,
    STORAGE_VERSION,
)
from custom_components.rest_mapper.lib import (
    Fetched,
    FetchError,
    FetchSpec,
    JsonValue,
    NotModified,
    Validators,
)
from custom_components.rest_mapper.mappings import mapping_data
from custom_components.rest_mapper.store import StoredEndpoint
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers.selector import SelectSelector
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from homeassistant.core import HomeAssistant, State

URL = "https://api.example.com/data?key=secret"
FIXTURES = Path(__file__).parent / "fixtures"

type Outcome = Fetched | NotModified | FetchError


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration."""


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return a state that must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def placeholders(result: Mapping[str, object]) -> Mapping[str, str]:
    """Return a form's description placeholders."""
    value = result["description_placeholders"]
    assert isinstance(value, dict)
    return value


def form_options(result: Mapping[str, object], field: str) -> list[str]:
    """Return the values a form's select field offers."""
    schema = result["data_schema"]
    assert isinstance(schema, vol.Schema)
    selector = schema.schema[field]
    assert isinstance(selector, SelectSelector)
    return [item if isinstance(item, str) else item["value"] for item in selector.config["options"]]


def fixture_json(name: str) -> JsonValue:
    """Read a recorded response."""
    decoded: JsonValue = json.loads((FIXTURES / name).read_text())
    return decoded


def response(body: JsonValue, etag: str | None = None) -> Fetched:
    """Build a `200`."""
    return Fetched(json.dumps(body).encode(), etag)


@dataclass
class FakeApi:
    """Answers from a list, repeating the last answer."""

    outcomes: list[Outcome] = field(default_factory=list)
    calls: int = 0
    validators: list[Validators | None] = field(default_factory=list)

    def will(self, *outcomes: Outcome) -> None:
        """Set the next answers."""
        self.outcomes = list(outcomes)

    async def __call__(
        self, session: object, spec: FetchSpec, validators: Validators | None = None
    ) -> Fetched | NotModified:
        """Stand in for the library's `fetch`."""
        self.calls += 1
        self.validators.append(validators)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, FetchError):
            raise outcome
        return outcome


@pytest.fixture
def api() -> Generator[FakeApi]:
    """Replace `fetch` everywhere it is used."""
    fake = FakeApi()
    with (
        patch("custom_components.rest_mapper.coordinator.fetch", fake),
        patch("custom_components.rest_mapper.config_flow.fetch", fake),
    ):
        yield fake


def sensor_subentry(path: str = "$.temp", title: str = "Temp") -> ConfigSubentryData:
    """Build a number sensor mapping."""
    return ConfigSubentryData(
        data=mapping_data("sensor", {"value": path}, {"value_type": "number"}, {}),
        subentry_type="sensor",
        title=title,
        unique_id=None,
    )


def make_entry(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    body: JsonValue,
    subentries: list[ConfigSubentryData],
    **options: object,
) -> MockConfigEntry:
    """Add an entry with stored data, as a finished setup leaves it."""
    storage_id = "test"
    now = dt_util.utcnow()
    stored = StoredEndpoint(body=body, has_body=True, confirmed_at=now)
    stored.policy.last_request = now
    hass_storage[f"{DOMAIN}.{storage_id}"] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": f"{DOMAIN}.{storage_id}",
        "data": stored.to_data(),
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="api.example.com",
        data={CONF_URL: URL, CONF_AUTH_TYPE: "none", CONF_STORAGE_ID: storage_id},
        options={CONF_MODE: "automatic", CONF_INTERVAL: 900, CONF_RETENTION: 0, **options},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    return entry


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Load an entry."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
