"""The kind table and the behaviour every kind shares: ha/entity-kinds."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from custom_components.rest_mapper.config_flow import RestMapperConfigFlow
from custom_components.rest_mapper.const import COLLECTION, DOMAIN
from custom_components.rest_mapper.kinds import KINDS, KindDefinition, Role, Shape, read_sensor
from custom_components.rest_mapper.lib import ConnectFailedError, JsonValue
from custom_components.rest_mapper.mappings import mapping_data
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import FakeApi, make_entry, response, start, state_of

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

Storage = dict[str, object]
BODY: JsonValue = {"v": 1, "lat": 50.0, "lon": 19.0, "extra": "x"}
ATTRIBUTES = {"extra": "$.extra"}
SINGLE_CASES = {
    "sensor": ({"value": "$.v"}, {"value_type": "number"}, "sensor.api_example_com_it"),
    "binary_sensor": ({"value": "$.v"}, {}, "binary_sensor.api_example_com_it"),
    "device_tracker": (
        {"latitude": "$.lat", "longitude": "$.lon"},
        {},
        "device_tracker.api_example_com_it",
    ),
}


def single(kind: str) -> tuple[ConfigSubentryData, str]:
    """Build a mapping of a kind, and its entity ID."""
    roles, settings, entity_id = SINGLE_CASES[kind]
    subentry = ConfigSubentryData(
        data=mapping_data(kind, roles, settings, ATTRIBUTES),
        subentry_type=kind,
        title="It",
        unique_id=None,
    )
    return subentry, entity_id


def test_every_single_kind_has_a_contract_case() -> None:
    kinds = {name for name, item in KINDS.items() if Shape.SINGLE in item.shapes}
    assert kinds == set(SINGLE_CASES)


@pytest.mark.parametrize("kind", SINGLE_CASES)
async def test_contract(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
    kind: str,
) -> None:
    subentry, entity_id = single(kind)
    entry = make_entry(hass, hass_storage, BODY, [subentry], retention_seconds=3600)
    await start(hass, entry)
    assert api.calls == 0

    before = state_of(hass, entity_id)
    assert before.attributes["extra"] == "x"
    assert "last_confirmed" in before.attributes

    api.will(ConnectFailedError("down"))
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id).state == before.state
    assert state_of(hass, entity_id).attributes["extra"] == "x"

    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id).state == "unavailable"

    platform = hass.data["entity_components"][kind].get_entity(entity_id)
    assert MATCH_ALL in platform._state_info["unrecorded_attributes"]

    hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(entity_id) is None


async def test_unchanged_tracker_writes_nothing(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    subentry, entity_id = single("device_tracker")
    await start(hass, make_entry(hass, hass_storage, BODY, [subentry], interval_seconds=60))
    api.will(response(BODY))
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    first = state_of(hass, entity_id).last_updated
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id).last_updated == first


def _test_kind(min_version: str = "2026.10.0") -> KindDefinition:
    """Build a kind on an existing platform."""
    return KindDefinition(
        "future",
        "sensor",
        frozenset({Shape.SINGLE}),
        (Role("value"),),
        read_sensor,
        min_version=min_version,
    )


def test_new_kind_is_offered() -> None:
    with patch.dict(KINDS, {"future": _test_kind()}):
        types = RestMapperConfigFlow.async_get_supported_subentry_types(None)  # type: ignore[arg-type]  # Unused.
    assert {"sensor", "binary_sensor", "device_tracker", COLLECTION, "future"} == set(types)


def test_kind_needing_a_newer_version_is_hidden() -> None:
    with patch.dict(KINDS, {"future": _test_kind("2099.1.0")}):
        types = RestMapperConfigFlow.async_get_supported_subentry_types(None)  # type: ignore[arg-type]  # Unused.
    assert "future" not in types


async def test_unknown_kind_is_skipped_and_reported(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    good, entity_id = single("sensor")
    unknown = ConfigSubentryData(
        data=mapping_data("from_the_future", {"value": "$.v"}, {}, {}),
        subentry_type="from_the_future",
        title="Later",
        unique_id=None,
    )
    entry = make_entry(hass, hass_storage, BODY, [good, unknown])
    await start(hass, entry)
    assert entry.state.name == "LOADED"
    assert state_of(hass, entity_id).state == "1"
    keys = {issue.translation_key for issue in ir.async_get(hass).issues.values()}
    assert "unknown_kind" in keys
    assert len(entry.subentries) == 2
    assert DOMAIN in {domain for domain, _ in ir.async_get(hass).issues}


ITEM_CASES = {
    "sensor": ({"value": "v"}, {"value_type": "number"}, "sensor.it_1"),
    "binary_sensor": ({"value": "v"}, {}, "binary_sensor.it_1"),
    "device_tracker": ({"latitude": "lat", "longitude": "lon"}, {}, "device_tracker.it_1"),
    "calendar": ({"start": "at", "summary": "s"}, {}, "calendar.it"),
}


def test_every_list_kind_has_a_contract_case() -> None:
    kinds = {
        name
        for name, item in KINDS.items()
        if item.shapes & {Shape.ITEM, Shape.WHOLE_LIST} and name != "geo_location"
    }
    assert kinds == set(ITEM_CASES)


@pytest.mark.parametrize("kind", ITEM_CASES)
async def test_list_contract(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
    kind: str,
) -> None:
    roles, settings, entity_id = ITEM_CASES[kind]
    body: JsonValue = {
        "items": [{"id": 1, "v": 1, "lat": 50, "lon": 19, "at": "2099-01-01", "s": "S"}]
    }
    subentry = ConfigSubentryData(
        data=mapping_data(
            kind, roles, {**settings, "events": False}, {}, list_path="$.items", key_path="id"
        ),
        subentry_type=COLLECTION,
        title="It",
        unique_id=None,
    )
    await start(hass, make_entry(hass, hass_storage, body, [subentry], retention_seconds=3600))
    assert api.calls == 0
    before = state_of(hass, entity_id).state

    api.will(ConnectFailedError("down"))
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id).state == before

    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id).state == "unavailable"
