"""Collections: ha/collection-entities."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from custom_components.rest_mapper.lib import ConnectFailedError, JsonValue
from custom_components.rest_mapper.mappings import mapping_data
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData, SubentryFlowResult
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from conftest import (
    FakeApi,
    fixture_json,
    form_options,
    make_entry,
    placeholders,
    response,
    start,
    state_of,
)

if TYPE_CHECKING:
    import pytest
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

Storage = dict[str, object]


def collection(
    kind: str,
    roles: dict[str, str],
    *,
    list_path: str = "$.items",
    key: str = "id",
    title: str = "Things",
    attributes: dict[str, str] | None = None,
    **settings: JsonValue,
) -> ConfigSubentryData:
    """Build a collection mapping."""
    return ConfigSubentryData(
        data=mapping_data(
            kind,
            roles,
            {"source": "things", "events": True, **settings},
            attributes or {},
            list_path=list_path,
            key_path=key,
        ),
        subentry_type="collection",
        title=title,
        unique_id=None,
    )


def items(*keys: int) -> JsonValue:
    """Build a response with items at one place."""
    return {"items": [{"id": key, "v": key * 10, "lat": 50, "lon": 19} for key in keys]}


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Move past the next request."""
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


SENSORS = collection("sensor", {"value": "v"}, value_type="number")


async def test_items_come_and_go(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, items(1, 2), [SENSORS]))
    assert api.calls == 0
    registry = er.async_get(hass)
    assert state_of(hass, "sensor.things_1").state == "10"
    assert state_of(hass, "sensor.things_item_count").state == "2"

    api.will(response(items(1, 2, 3)))
    await poll(hass, freezer)
    assert state_of(hass, "sensor.things_3").state == "30"

    api.will(response(items(1, 3)))
    await poll(hass, freezer)
    assert hass.states.get("sensor.things_2") is not None
    await poll(hass, freezer)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.things_2") is None
    assert registry.async_get("sensor.things_2") is None
    assert state_of(hass, "sensor.things_item_count").state == "2"


async def test_entity_id_after_return(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", removal_after=1)
    await start(hass, make_entry(hass, hass_storage, items(1, 2), [entry]))
    api.will(response(items(1)), response(items(1, 2)))
    await poll(hass, freezer)
    assert hass.states.get("sensor.things_2") is None
    await poll(hass, freezer)
    assert state_of(hass, "sensor.things_2").state == "20"


async def test_outage_removes_nothing(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, items(1), [SENSORS]))
    api.will(ConnectFailedError("down"))
    for _ in range(4):
        freezer.tick(timedelta(hours=7))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert state_of(hass, "sensor.things_1").state == "10"


async def test_binary_items(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = collection("binary_sensor", {"value": "v"}, on_values="10")
    await start(hass, make_entry(hass, hass_storage, items(1, 2), [entry]))
    assert state_of(hass, "binary_sensor.things_1").state == "on"
    assert state_of(hass, "binary_sensor.things_2").state == "off"


async def test_silesia_alert_as_trackers(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = collection(
        "device_tracker",
        {"name": "name", "latitude": "lat", "longitude": "lon"},
        list_path="$",
        title="Sirens",
        attributes={"status": "currentStatus"},
        item_limit=100,
    )
    await start(hass, make_entry(hass, hass_storage, fixture_json("silesia_alert.json"), [entry]))
    state = state_of(hass, "device_tracker.sirens_mikolow")
    assert state.attributes["latitude"] == 50.168
    assert "status" in state.attributes
    assert state_of(hass, "sensor.sirens_item_count").state == "61"
    item = er.async_get(hass).async_get("device_tracker.sirens_mikolow")
    assert item is not None
    assert not item.disabled


async def test_aircraft_leave_no_registry_entries(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    hass.config.latitude = 50
    hass.config.longitude = 19
    body = fixture_json("adsb_lol.json")
    entry = collection(
        "geo_location",
        {"name": "flight", "latitude": "lat", "longitude": "lon"},
        list_path="$.ac",
        key="hex",
        title="Planes",
        removal_after=1,
    )
    await start(hass, make_entry(hass, hass_storage, body, [entry]))
    positions = hass.states.async_all("geo_location")
    assert positions
    assert positions[0].attributes["source"] == "rest_mapper_things"
    assert float(positions[0].state) > 0
    registry = er.async_get(hass)
    assert all(registry.async_get(state.entity_id) is None for state in positions)

    api.will(response({"ac": []}))
    await poll(hass, freezer)
    await hass.async_block_till_done()
    assert hass.states.async_all("geo_location") == []


async def test_tomtom_shaped_line(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    body: JsonValue = {
        "incidents": [
            {
                "type": "Feature",
                "id": "a",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[19.0, 50.0], [19.1, 50.1], [19.2, 50.2]],
                },
            }
        ]
    }
    entry = collection("geo_location", {"geometry": "geometry"}, list_path="$.incidents")
    await start(hass, make_entry(hass, hass_storage, body, [entry]))
    state = hass.states.async_all("geo_location")[0]
    assert state.attributes["latitude"] == 50.1
    assert state.attributes["longitude"] == 19.1


async def test_item_with_no_position_is_skipped(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    body: JsonValue = {"items": [{"id": 1, "lat": 50, "lon": 19}, {"id": 2}]}
    entry = collection("geo_location", {"latitude": "lat", "longitude": "lon"})
    await start(hass, make_entry(hass, hass_storage, body, [entry]))
    assert len(hass.states.async_all("geo_location")) == 1
    assert state_of(hass, "sensor.things_item_count").state == "2"


async def test_position_lost_keeps_the_last_one(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("geo_location", {"latitude": "lat", "longitude": "lon"})
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    api.will(response({"items": [{"id": 1, "lat": None, "lon": None}]}))
    await poll(hass, freezer)
    assert hass.states.async_all("geo_location")[0].attributes["latitude"] == 50


async def test_positions_leave_past_the_retention_limit(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("geo_location", {"latitude": "lat", "longitude": "lon"})
    made = make_entry(hass, hass_storage, items(1), [entry], retention_seconds=3600)
    await start(hass, made)
    api.will(ConnectFailedError("down"))
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.async_all("geo_location") == []
    api.will(response(items(1)))
    freezer.tick(timedelta(hours=7))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(hass.states.async_all("geo_location")) == 1


async def test_new_item_event(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", attributes={"lat": "lat"})
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    state_changes = async_capture_events(hass, "state_changed")
    api.will(response(items(1, 2)))
    await poll(hass, freezer)
    event = state_of(hass, "event.things_new_item")
    assert event.attributes["key"] == "2"
    assert event.attributes["lat"] == 50
    fired = [item for item in state_changes if item.data["entity_id"] == "event.things_new_item"]
    assert len(fired) == 1


async def test_event_flood(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, items(1), [SENSORS]))
    state_changes = async_capture_events(hass, "state_changed")
    api.will(response(items(1, *range(2, 42))))
    await poll(hass, freezer)
    fired = [item for item in state_changes if item.data["entity_id"] == "event.things_new_item"]
    assert len(fired) == 10
    assert fired[-1].data["new_state"].attributes["left_out"] == 30


async def test_no_event_after_restart_and_one_for_downtime(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = make_entry(hass, hass_storage, items(1), [SENSORS])
    await start(hass, entry)
    api.will(response(items(1)))
    await poll(hass, freezer)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state_changes = async_capture_events(hass, "state_changed")
    await poll(hass, freezer)
    fired = [
        item
        for item in state_changes
        if item.data["entity_id"] == "event.things_new_item"
        and item.data["new_state"].state not in ("unknown", "unavailable")
    ]
    assert fired == []

    api.will(response(items(1, 2)))
    await poll(hass, freezer)
    assert state_of(hass, "event.things_new_item").attributes["key"] == "2"


async def test_events_can_be_off(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", events=False)
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    assert hass.states.get("event.things_new_item") is None


async def test_item_over_the_limit_fires_nothing(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", item_limit=1)
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    api.will(response(items(1, 2)))
    await poll(hass, freezer)
    assert state_of(hass, "event.things_new_item").state == "unknown"


CALENDAR = collection(
    "calendar",
    {"start": "date", "summary": "kind", "end": "until"},
    naive_local=True,
    title="Waste",
    events=False,
)


async def test_calendar_from_dates(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    tomorrow = (dt_util.now() + timedelta(days=1)).date().isoformat()
    body: JsonValue = {"items": [{"id": 1, "date": tomorrow, "kind": "Paper"}]}
    await start(hass, make_entry(hass, hass_storage, body, [CALENDAR]))
    state = state_of(hass, "calendar.waste")
    assert state.state == "off"
    assert state.attributes["message"] == "Paper"
    assert state.attributes["all_day"] is True


async def test_calendar_listing_sends_nothing(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    start_time = dt_util.now() + timedelta(hours=1)
    body: JsonValue = {
        "items": [
            {"id": 1, "date": start_time.isoformat(), "kind": "Timed"},
            {"id": 2, "date": "nonsense", "kind": "Broken"},
            {
                "id": 3,
                "date": start_time.isoformat(),
                "kind": "Bad end",
                "until": "2000-01-01T00:00:00+00:00",
            },
        ]
    }
    await start(hass, make_entry(hass, hass_storage, body, [CALENDAR]))
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.waste", "duration": {"days": 1}},
        blocking=True,
        return_response=True,
    )
    assert result is not None
    response_data = result["calendar.waste"]
    assert isinstance(response_data, dict)
    events = response_data["events"]
    assert isinstance(events, list)
    summaries: list[str] = []
    ends: list[str] = []
    for event in events:
        assert isinstance(event, dict)
        summary, end = event["summary"], event["end"]
        assert isinstance(summary, str)
        assert isinstance(end, str)
        summaries.append(summary)
        ends.append(end)
    assert summaries == ["Timed", "Bad end"]
    assert datetime.fromisoformat(ends[0]) - start_time == timedelta(hours=1)
    assert api.calls == 0


async def test_calendar_turns_on_with_no_request(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    start_time = dt_util.now() + timedelta(minutes=5)
    body: JsonValue = {"items": [{"id": 1, "date": start_time.isoformat(), "kind": "Now"}]}
    await start(hass, make_entry(hass, hass_storage, body, [CALENDAR]))
    api.will(response(body))
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, "calendar.waste").state == "on"
    assert api.calls == 0


async def add(hass: HomeAssistant, entry: MockConfigEntry) -> SubentryFlowResult:
    """Start adding a collection."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "collection"), context={"source": SOURCE_USER}
    )


async def step(
    hass: HomeAssistant, result: SubentryFlowResult, data: dict[str, object]
) -> SubentryFlowResult:
    """Submit a step."""
    return await hass.config_entries.subentries.async_configure(result["flow_id"], data)


async def test_flow_for_silesia_alert(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    body = fixture_json("silesia_alert.json")
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    result = await add(hass, entry)
    assert result["step_id"] == "user"
    options = form_options(result, "list_path")
    assert options == ["$"]
    result = await step(hass, result, {"list_path": "$"})
    assert result["step_id"] == "key"
    keys = form_options(result, "key_path")
    assert keys[0] == "id"
    result = await step(hass, result, {"key_path": "id"})
    result = await step(hass, result, {"kind": "device_tracker"})
    assert result["step_id"] == "item"
    result = await step(
        hass,
        result,
        {"title": "Sirens", "name": "name", "latitude": "lat", "longitude": "lon"},
    )
    assert result["step_id"] == "limits"
    result = await step(hass, result, {"item_limit": 100, "removal_after": 2, "events": True})
    assert result["step_id"] == "attributes"
    result = await step(hass, result, {"attributes": [{"path": "currentStatus"}]})
    assert result["step_id"] == "confirm"
    assert placeholders(result)["preview"].startswith("Items: 61, skipped: 0")
    result = await step(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.sirens_item_count").state == "61"
    assert api.calls == 0


async def test_adding_a_platform_reloads_cleanly(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = make_entry(hass, hass_storage, items(1), [])
    await start(hass, entry)
    result = await add(hass, entry)
    result = await step(hass, result, {"list_path": "$.items"})
    result = await step(hass, result, {"key_path": "id"})
    result = await step(hass, result, {"kind": "geo_location"})
    result = await step(hass, result, {"title": "P", "latitude": "lat", "longitude": "lon"})
    result = await step(hass, result, {"item_limit": 5, "removal_after": 1, "events": True})
    result = await step(hass, result, {"attributes": []})
    result = await step(hass, result, {})
    result = await step(hass, result, {})
    await hass.async_block_till_done()
    assert "Ignored unload request" not in caplog.text


async def test_flow_empty_list_takes_typed_paths(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"ac": []}, [])
    await start(hass, entry)
    result = await add(hass, entry)
    result = await step(hass, result, {"list_path": "$.ac"})
    result = await step(hass, result, {"key_path": "hex"})
    result = await step(hass, result, {"kind": "geo_location"})
    result = await step(hass, result, {"title": "Planes", "latitude": "lat", "longitude": "lon"})
    result = await step(hass, result, {"item_limit": 50, "removal_after": 1, "events": False})
    result = await step(hass, result, {"attributes": []})
    assert "Items: 0" in placeholders(result)["preview"]
    result = await step(hass, result, {})
    assert result["step_id"] == "map_hint"
    assert placeholders(result)["source"] == "rest_mapper_planes"
    result = await step(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_flow_calendar_has_no_attributes(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    body: JsonValue = {"items": [{"id": 1, "date": "2030-01-01", "kind": "Paper"}]}
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    result = await add(hass, entry)
    result = await step(hass, result, {"list_path": "$.items"})
    result = await step(hass, result, {"key_path": "id"})
    result = await step(hass, result, {"kind": "calendar"})
    result = await step(hass, result, {"title": "Waste", "start": "date", "summary": "kind"})
    assert result["step_id"] == "settings"
    result = await step(hass, result, {"naive_local": True})
    result = await step(hass, result, {"item_limit": 50, "removal_after": 2, "events": False})
    assert result["step_id"] == "confirm"
    await step(hass, result, {})
    await hass.async_block_till_done()
    assert state_of(hass, "calendar.waste").attributes["message"] == "Paper"


async def test_everything_survives_a_restart(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    tomorrow = (dt_util.now() + timedelta(days=1)).date().isoformat()
    moment = (dt_util.now() + timedelta(days=2)).isoformat()
    body: JsonValue = {
        "items": [
            {"id": 1, "lat": 50, "lon": 19, "date": tomorrow, "at": moment, "kind": "Paper"},
        ]
    }
    subentries = [
        collection("device_tracker", {"latitude": "lat", "longitude": "lon"}, title="Cars"),
        collection("sensor", {"value": "at"}, title="Times", value_type="timestamp"),
        CALENDAR,
    ]
    entry = make_entry(hass, hass_storage, body, subentries)
    await start(hass, entry)
    api.will(response(body))
    await poll(hass, freezer)
    before = state_of(hass, "sensor.times_1").state
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, "device_tracker.cars_1").attributes["latitude"] == 50
    assert state_of(hass, "sensor.times_1").state == before
    assert state_of(hass, "calendar.waste").attributes["message"] == "Paper"


async def test_key_named_count_keeps_its_entity(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    body: JsonValue = {"items": [{"id": "count", "v": 5}]}
    await start(hass, make_entry(hass, hass_storage, body, [SENSORS]))
    assert state_of(hass, "sensor.things_count").state == "5"
    assert state_of(hass, "sensor.things_item_count").state == "1"


async def test_changing_the_kind_removes_old_items(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, items(1), [SENSORS])
    await start(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.things_1") is not None
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data=mapping_data(
            "binary_sensor",
            {"value": "v"},
            {"on_values": "10", "source": "things"},
            {},
            list_path="$.items",
            key_path="id",
        ),
    )
    await hass.async_block_till_done()
    assert registry.async_get("sensor.things_1") is None
    assert registry.async_get("event.things_new_item") is None
    assert state_of(hass, "binary_sensor.things_1").state == "on"
