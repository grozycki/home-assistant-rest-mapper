"""Requests, retention and persistence: ha/request-control and ha/data-retention."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from custom_components.rest_mapper.const import DOMAIN
from custom_components.rest_mapper.lib import (
    AuthFailedError,
    ConnectFailedError,
    NotModified,
    RateLimitedError,
    ServerFailedError,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import FakeApi, make_entry, response, sensor_subentry, start, state_of

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

Storage = dict[str, object]
ENTITY = "sensor.api_example_com_temp"
STARTED = "2000-01-01"
STATUS = "sensor.api_example_com_status"
STALE = "binary_sensor.api_example_com_data_problem"
REFRESH = "button.api_example_com_refresh"


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float = 16) -> None:
    """Move past the next request."""
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_no_request_at_startup(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    assert api.calls == 0
    assert state_of(hass, ENTITY).state == "21.5"


async def test_endpoint_down_at_startup(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(ConnectFailedError("down"))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    assert entry.state.name == "LOADED"
    assert state_of(hass, ENTITY).state == "21.5"


async def test_reload_sends_nothing_and_keeps_the_schedule(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    for _ in range(5):
        freezer.tick(timedelta(minutes=1))
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert api.calls == 0
    await poll(hass, freezer, 11)
    assert api.calls == 1


async def test_schedule_and_new_value(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    assert api.calls == 1
    assert state_of(hass, ENTITY).state == "22"


async def test_failure_keeps_the_value_and_backs_off(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(ServerFailedError(500, None))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    assert api.calls == 1
    assert state_of(hass, ENTITY).state == "21.5"
    assert state_of(hass, STATUS).state == "retrying"
    assert state_of(hass, STALE).state == "on"
    await poll(hass, freezer, 16)
    assert api.calls == 1
    await poll(hass, freezer, 20)
    assert api.calls == 2


async def test_rate_limit(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(RateLimitedError("7200"))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    assert state_of(hass, STATUS).state == "rate_limited"
    assert state_of(hass, ENTITY).state == "21.5"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    assert api.calls == 1


async def test_not_modified(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}, etag='"a"'), NotModified())
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    first = state_of(hass, "sensor.api_example_com_last_confirmed_data").state
    await poll(hass, freezer)
    assert api.validators[1] is not None
    assert api.validators[1].etag == '"a"'
    assert state_of(hass, ENTITY).state == "22"
    assert state_of(hass, "sensor.api_example_com_last_confirmed_data").state != first


async def test_auth_failure_starts_reauth_and_stops(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(AuthFailedError(401))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert state_of(hass, STATUS).state == "auth_failed"
    await poll(hass, freezer, 600)
    assert api.calls == 1


async def test_on_demand(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()], mode="on_demand")
    await start(hass, entry)
    await poll(hass, freezer, 6000)
    assert api.calls == 0
    assert state_of(hass, STATUS).state == "on_demand"
    await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    await hass.async_block_till_done()
    assert api.calls == 1
    assert state_of(hass, ENTITY).state == "22"


async def test_update_of_many_entities_is_one_request(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response({"temp": 22, "b": 1}))
    subentries = [sensor_subentry(), sensor_subentry("$.b", "B")]
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5, "b": 2}, subentries))
    await async_setup_component(hass, "homeassistant", {})
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": [ENTITY, "sensor.api_example_com_b"]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert api.calls == 1


async def test_budget_used_up(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(
        hass,
        hass_storage,
        {"temp": 21.5},
        [sensor_subentry()],
        budget_limit=1,
        budget_period="day",
        reserve_percent=0,
    )
    await start(hass, entry)
    await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.api_example_com_requests_used").state == "1"
    assert state_of(hass, "sensor.api_example_com_requests_remaining").state == "0"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    assert api.calls == 1


async def test_one_field_vanishes(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"b": 3}))
    subentries = [sensor_subentry(), sensor_subentry("$.b", "B")]
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5, "b": 2}, subentries))
    before = state_of(hass, ENTITY).attributes["last_confirmed"]
    await poll(hass, freezer)
    assert state_of(hass, ENTITY).state == "21.5"
    assert state_of(hass, ENTITY).attributes["last_confirmed"] == before
    assert state_of(hass, "sensor.api_example_com_b").state == "3"


async def test_null_is_unknown(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": None}))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    await poll(hass, freezer)
    assert state_of(hass, ENTITY).state == "unknown"


async def test_retention_limit(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(ConnectFailedError("down"))
    entry = make_entry(
        hass, hass_storage, {"temp": 21.5}, [sensor_subentry()], retention_seconds=3600
    )
    await start(hass, entry)
    await poll(hass, freezer, 30)
    assert state_of(hass, ENTITY).state == "21.5"
    await poll(hass, freezer, 40)
    assert state_of(hass, ENTITY).state == "unavailable"
    api.will(response({"temp": 25}))
    await poll(hass, freezer, 600)
    assert state_of(hass, ENTITY).state == "25"


async def test_values_survive_a_restart(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    await poll(hass, freezer)
    api.will(response({"b": 1}))
    await poll(hass, freezer)
    confirmed = state_of(hass, ENTITY).attributes["last_confirmed"]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = state_of(hass, ENTITY)
    assert state.state == "22"
    assert state.attributes["last_confirmed"] == confirmed


async def test_saves_are_delayed(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    await start(hass, make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()]))
    stored_before = hass_storage[f"{DOMAIN}.test"]
    await poll(hass, freezer, 1)
    assert hass_storage[f"{DOMAIN}.test"] is stored_before


async def test_entry_removal_deletes_storage(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.test" not in hass_storage


async def test_saves_happen_with_short_intervals(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()], interval_seconds=60)
    await start(hass, entry)
    for _ in range(8):
        await poll(hass, freezer, 1.1)
    written = hass_storage[f"{DOMAIN}.test"]
    assert isinstance(written, dict)
    assert written["data"]["policy"]["last_request"] != STARTED


async def test_poll_in_flight_during_reload_leaves_nothing_running(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    old = entry.runtime_data
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not old
    old._schedule_next()
    old._schedule_expiry(old.data.confirmed_at)
    old._save_later()
    assert old._cancel_timer is None
    assert old._cancel_expiry is None
    assert not old._save_pending


async def test_reauth_returns_after_a_restart(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(AuthFailedError(401))
    entry = make_entry(hass, hass_storage, {"temp": 21.5}, [sensor_subentry()])
    await start(hass, entry)
    await poll(hass, freezer)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_on_demand_expiry_follows_the_new_data(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22}))
    entry = make_entry(
        hass,
        hass_storage,
        {"temp": 21.5},
        [sensor_subentry()],
        mode="on_demand",
        retention_seconds=3600,
    )
    await start(hass, entry)
    await poll(hass, freezer, 50)
    await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    await hass.async_block_till_done()
    await poll(hass, freezer, 30)
    assert state_of(hass, ENTITY).state == "22"
    await poll(hass, freezer, 31)
    assert state_of(hass, ENTITY).state == "unavailable"
