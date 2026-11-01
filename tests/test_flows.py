"""Setup and mapping flows: ha/endpoint-setup, ha/field-entities, ha/attribute-mapping."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from custom_components.rest_mapper.const import DOMAIN
from custom_components.rest_mapper.lib import (
    AuthFailedError,
    ConnectFailedError,
    FetchError,
    FormatError,
    JsonValue,
    RateLimitedError,
)
from custom_components.rest_mapper.mappings import mapping_data
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    SubentryFlowResult,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from conftest import (
    URL,
    FakeApi,
    fixture_json,
    make_entry,
    placeholders,
    response,
    sensor_subentry,
    start,
    state_of,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

Storage = dict[str, object]
BODY: JsonValue = {"current": {"temp_c": 21.5, "open": True}, "name": "x"}
FOUND: dict[str, object] = {"polling": "900"}


async def begin(hass: HomeAssistant, url: str = URL) -> ConfigFlowResult:
    """Start setup and submit the first step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"url": url})


async def finish(
    hass: HomeAssistant, result: ConfigFlowResult, data: dict[str, object] | None = None
) -> ConfigFlowResult:
    """Confirm the found screen."""
    final = await hass.config_entries.flow.async_configure(result["flow_id"], data or FOUND)
    await hass.async_block_till_done()
    return final


async def test_public_endpoint_in_two_screens(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(fixture_json("silesia_alert.json"), etag='"a"'))
    result = await begin(hass, "https://silesiaalert.pl/dane5.json")
    assert result["step_id"] == "found"
    schema = result["data_schema"]
    assert schema is not None
    defaults = {
        str(marker): marker.default()
        for marker in schema.schema
        if str(marker) == "selected_fields"
    }
    result = await finish(hass, result, {**FOUND, **defaults})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert api.calls == 1
    assert state_of(hass, "sensor.silesiaalert_item_count").state == "61"
    assert state_of(hass, "device_tracker.silesiaalert_mikolow").attributes["latitude"] == 50.168


async def test_found_offers_words_and_cost(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(fixture_json("silesia_alert.json")))
    result = await begin(hass, "https://silesiaalert.pl/dane5.json")
    schema = result["data_schema"]
    assert schema is not None
    labels = {
        str(marker): [item["label"] for item in selector.config["options"]]
        for marker, selector in schema.schema.items()
        if str(marker) in ("selected_fields", "polling")
    }
    assert labels["selected_fields"][0].startswith("silesiaalert: 61 places on the map")
    assert "2880" in labels["polling"][1]


async def test_fields_of_a_flat_response(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    result = await begin(hass)
    result = await finish(
        hass,
        result,
        {**FOUND, "selected_fields": ["field:$.current.temp_c", "field:$.current.open"]},
    )
    entry = result["result"]
    assert {item.subentry_type for item in entry.subentries.values()} == {
        "sensor",
        "binary_sensor",
    }
    temperature = state_of(hass, "sensor.api_example_com_temp_c")
    assert temperature.state == "21.5"
    assert temperature.attributes["unit_of_measurement"] == "°C"


async def test_key_in_the_url_becomes_a_credential(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    result = await begin(hass, "https://api.example.com/data?a=1&key=marker")
    assert "marker" not in str(result)
    result = await finish(hass, result)
    entry = result["result"]
    assert "marker" not in entry.data["url"]
    assert "marker" not in entry.title
    assert (entry.data["auth_type"], entry.data["auth_name"]) == ("query", "key")
    assert entry.data["auth_value"] == "marker"


async def test_authentication_only_after_a_refusal(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(AuthFailedError(401), response(BODY))
    result = await begin(hass, "https://api.example.com/data")
    assert result["step_id"] == "auth"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_type": "bearer", "auth_value": "abc"}
    )
    assert result["step_id"] == "found"


async def test_refused_key_stays_on_the_form(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(AuthFailedError(401))
    result = await begin(hass, "https://api.example.com/data")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_type": "bearer", "auth_value": "abc"}
    )
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_header_needs_a_name(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(AuthFailedError(401))
    result = await begin(hass, "https://api.example.com/data")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_type": "header", "auth_value": "abc"}
    )
    assert result["errors"] == {"auth_name": "name_required"}


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ConnectFailedError("x"), "cannot_connect"),
        (RateLimitedError(None), "rate_limited"),
        (FormatError("x"), "not_json"),
    ],
)
async def test_failed_test_request(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    failure: FetchError,
    key: str,
) -> None:
    api.will(failure)
    result = await begin(hass)
    assert result["errors"] == {"base": key}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("ftp://example.com", "invalid_url"),
        ("nonsense", "invalid_url"),
        ("https://user:pass@example.com/", "credentials_in_url"),
    ],
)
async def test_bad_url(hass: HomeAssistant, api: FakeApi, url: str, key: str) -> None:
    result = await begin(hass, url)
    assert result["errors"] == {"url": key}
    assert api.calls == 0


async def test_credentials_on_plain_http(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    result = await begin(hass, "http://api.example.com/?key=abc")
    assert result["step_id"] == "insecure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "found"


async def test_new_address_needs_a_new_warning(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(ConnectFailedError("x"))
    result = await begin(hass, "http://one.example.com/?key=abc")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://two.example.com/?key=abc"}
    )
    assert result["step_id"] == "insecure"


async def test_nothing_recognised(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    api.will(response([]))
    result = await begin(hass)
    assert "Nothing could be recognised" in placeholders(result)["note"]
    result = await finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("answers", "mode", "interval", "budget"),
    [
        ({"polling": "3600"}, "automatic", 3600, 0),
        ({"polling": "on_demand"}, "on_demand", 900, 0),
        ({"polling": "900", "budget_limit": 2500}, "automatic", 900, 2500),
        ({"polling": "900", "advanced": {"interval_seconds": 120}}, "automatic", 120, 0),
    ],
)
async def test_polling_choices(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    answers: dict[str, object],
    mode: str,
    interval: int,
    budget: int,
) -> None:
    api.will(response(BODY))
    result = await finish(hass, await begin(hass), answers)
    options = result["result"].options
    assert (options["mode"], options["interval_seconds"], options["budget_limit"]) == (
        mode,
        interval,
        budget,
    )


async def test_same_endpoint_twice(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    await finish(hass, await begin(hass))
    result = await begin(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_same_url_with_another_key(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    await finish(hass, await begin(hass, "https://api.example.com/d?key=one"))
    result = await begin(hass, "https://api.example.com/d?key=two")
    assert result["step_id"] == "found"


async def test_reauth_keeps_the_budget_and_resumes(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    entry = make_entry(hass, hass_storage, BODY, [], budget_limit=100, budget_period="month")
    await start(hass, entry)
    coordinator = entry.runtime_data
    coordinator.policy.record_sent()
    coordinator.policy.record_auth_failed()
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.policy.used == 2
    assert not coordinator.policy.state.auth_stopped


async def test_reconfigure_keeps_the_stored_key(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    entry = make_entry(hass, hass_storage, BODY, [])
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "auth_type": "query", "auth_name": "key", "auth_value": "stored"},
    )
    await start(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://other.example.com/"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["auth_value"] == "stored"
    assert entry.data["url"] == "https://other.example.com/"


async def test_failed_reconfigure_changes_nothing(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(ConnectFailedError("x"))
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://other.example.com/"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data["url"] == URL
    assert entry.runtime_data.stored.body == BODY


async def test_reconfigure_replaces_the_sample(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response({"new": 1}))
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://other.example.com/"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["url"] == "https://other.example.com/"
    assert entry.runtime_data.stored.body == {"new": 1}


async def test_options_send_nothing(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    entry = make_entry(hass, hass_storage, BODY, [sensor_subentry("$.current.temp_c")])
    await start(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "mode": "on_demand",
            "interval_seconds": 900,
            "budget_limit": 0,
            "budget_period": "month",
            "reserve_percent": 10,
            "retention_seconds": 0,
            "max_response_kib": 2048,
            "timeout_seconds": 15,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert api.calls == 0
    assert entry.runtime_data.status().value == "on_demand"
    assert state_of(hass, "sensor.api_example_com_temp").state == "21.5"


async def add(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> SubentryFlowResult:
    """Start adding a mapping."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": SOURCE_USER}
    )


async def step(
    hass: HomeAssistant, result: SubentryFlowResult, data: dict[str, object]
) -> SubentryFlowResult:
    """Submit a mapping step."""
    return await hass.config_entries.subentries.async_configure(result["flow_id"], data)


def adv(**values: object) -> dict[str, object]:
    """Wrap answers of the advanced section."""
    return {"advanced": values}


async def test_add_sensor_by_label_with_suggestions(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    assert result["step_id"] == "user"
    result = await step(hass, result, {"selected_fields": ["$.current.temp_c"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert api.calls == 0
    state = state_of(hass, "sensor.api_example_com_temp_c")
    assert state.state == "21.5"
    assert state.attributes["unit_of_measurement"] == "°C"


async def test_several_fields_at_once(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.current.temp_c", "$.name"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2
    assert state_of(hass, "sensor.api_example_com_name").state == "x"
    assert state_of(hass, "sensor.api_example_com_temp_c").state == "21.5"


async def test_mapping_added_during_a_reload_is_not_lost(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    subentries = [sensor_subentry("$.current.temp_c")]
    entry = make_entry(hass, hass_storage, BODY, subentries)
    await start(hass, entry)
    reload = hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    await asyncio.sleep(0)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(sensor_subentry("$.current.temp_c", "Second")["data"]),
            subentry_type="sensor",
            title="Second",
            unique_id=None,
        ),
    )
    await reload
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.api_example_com_second").state == "21.5"


async def test_several_fields_are_checked(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.name", "$.later"]})
    assert result["errors"] == {"base": "nothing_read"}
    assert len(entry.subentries) == 0


async def test_typed_text_path_past_the_listing(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    rows: list[JsonValue] = [{"t": str(index)} for index in range(26)]
    body: JsonValue = {"items": rows}
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.items[25].t"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_sensor_with_advanced_options(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(
        hass,
        result,
        {
            "selected_fields": ["$.current.temp_c"],
            **adv(title="Outside", attributes=[{"path": "$.name"}]),
        },
    )
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.api_example_com_outside").attributes["name"] == "x"


async def test_invalid_path(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.items[*]"]})
    assert result["errors"] == {"base": "invalid_path"}


async def test_nothing_chosen(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {})
    assert result["errors"] == {"base": "choose_field"}


async def test_unit_does_not_fit(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(
        hass,
        result,
        {
            "selected_fields": ["$.current.temp_c"],
            **adv(device_class="temperature", unit_of_measurement="kg"),
        },
    )
    assert result["errors"] == {"base": "unit_mismatch"}


async def test_wrong_type_is_refused(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.name"], **adv(value_type="number")})
    assert result["errors"] == {"base": "wrong_data"}
    assert placeholders(result)["summary"] == "This is the wrong kind of data."


async def test_missing_path_can_be_saved_anyway(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    answers: dict[str, object] = {"selected_fields": ["$.later"]}
    result = await step(hass, result, answers)
    assert result["errors"] == {"base": "nothing_read"}
    result = await step(hass, result, {**answers, "save_anyway": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_summary_lists_attributes_left_out(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"v": "x"}, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(
        hass,
        result,
        {
            "selected_fields": ["$.v"],
            **adv(value_type="number", attributes=[{"path": "$.gone"}]),
        },
    )
    assert "Attribute not found: gone" in placeholders(result)["summary"]


@pytest.mark.parametrize(
    ("rows", "key"),
    [
        ([{"name": "latitude", "path": "$.name"}], "reserved_attribute"),
        ([{"name": "a", "path": "$.x"}, {"name": "a", "path": "$.y"}], "duplicate_attribute"),
        ([{"name": "Bad Name", "path": "$.x"}], "invalid_attribute_name"),
        ([{"path": f"$.k{index}"} for index in range(21)], "too_many_attributes"),
        ([{"name": "a", "path": "$.a[*]"}], "invalid_path"),
    ],
)
async def test_attribute_errors(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    rows: list[dict[str, str]],
    key: str,
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.name"], **adv(attributes=rows)})
    assert result["errors"] == {"base": key}


@pytest.mark.parametrize(
    "settings",
    [
        {"value_type": "text", "device_class": "power_factor"},
        {"value_type": "text", "state_class": "measurement"},
        {"value_type": "text", "unit_of_measurement": "kg"},
        {"value_type": "number", "device_class": "timestamp"},
    ],
)
async def test_value_type_must_fit(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    settings: dict[str, object],
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"selected_fields": ["$.name"], **adv(**settings)})
    assert result["errors"] == {"base": "value_type_mismatch"}


async def test_binary_sensor_with_own_values(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"door": "closed"}, [])
    await start(hass, entry)
    result = await add(hass, entry, "binary_sensor")
    await step(
        hass,
        result,
        {"selected_fields": ["$.door"], **adv(on_values="open", off_values="closed")},
    )
    await hass.async_block_till_done()
    assert state_of(hass, "binary_sensor.api_example_com_door").state == "off"


async def test_tracker(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    body: JsonValue = {"v": {"lat": 50.26, "lon": 19.02}}
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    result = await add(hass, entry, "device_tracker")
    result = await step(
        hass, result, {"title": "Car", "latitude": "$.v.lat", "longitude": "$.v.lon"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    state = state_of(hass, "device_tracker.api_example_com_car")
    assert state.attributes["latitude"] == 50.26
    item = er.async_get(hass).async_get("device_tracker.api_example_com_car")
    assert item is not None
    assert item.entity_category is None
    assert not item.disabled


async def test_tracker_needs_a_position(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "device_tracker")
    result = await step(hass, result, {"title": "Car", "latitude": "$.name"})
    assert result["errors"] == {"base": "position_required"}


async def test_refresh_during_a_wait(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    entry.runtime_data.policy.record_rate_limited("3600")
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, adv(refresh_sample=True))
    assert result["errors"] == {"base": "refresh_refused"}
    assert api.calls == 0


async def test_remove_mapping_removes_the_entity(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [sensor_subentry("$.current.temp_c")])
    await start(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.api_example_com_temp")
    subentry_id = next(iter(entry.subentries))
    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    assert registry.async_get("sensor.api_example_com_temp") is None
    assert api.calls == 0


async def test_shared_device_survives_removal(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    subentries = [sensor_subentry("$.current.temp_c"), sensor_subentry("$.name", "Name")]
    entry = make_entry(hass, hass_storage, BODY, subentries)
    await start(hass, entry)
    hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.api_example_com_name") is not None


async def test_editing_keeps_settings_of_an_unopened_section(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    subentry = ConfigSubentryData(
        data=mapping_data(
            "sensor",
            {"value": "$.current.temp_c"},
            {"value_type": "number", "device_class": "temperature", "unit_of_measurement": "°C"},
            {"where": "$.name"},
        ),
        subentry_type="sensor",
        title="Temp",
        unique_id=None,
    )
    entry = make_entry(hass, hass_storage, BODY, [subentry])
    await start(hass, entry)
    result = await entry.start_subentry_reconfigure_flow(hass, next(iter(entry.subentries)))
    result = await step(hass, result, {"title": "Outside", "value": "$.current.temp_c"})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    state = state_of(hass, "sensor.api_example_com_temp")
    assert state.attributes["unit_of_measurement"] == "°C"
    assert state.attributes["where"] == "x"


async def test_edit_keeps_the_entity_id(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [sensor_subentry("$.current.temp_c")])
    await start(hass, entry)
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "user"
    result = await step(
        hass,
        result,
        {"title": "Temp", "value": "$.current.open", **adv(value_type="text")},
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.api_example_com_temp").state == "true"


async def test_date_on_a_timestamp_sensor(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    subentry = ConfigSubentryData(
        data=mapping_data(
            "sensor", {"value": "$.d"}, {"value_type": "timestamp", "device_class": "timestamp"}, {}
        ),
        subentry_type="sensor",
        title="D",
        unique_id=None,
    )
    subentries = [subentry, sensor_subentry("$.v", "V")]
    await start(hass, make_entry(hass, hass_storage, {"d": "2030-01-01", "v": 1}, subentries))
    assert state_of(hass, "sensor.api_example_com_d").state.startswith("2030-01-01T")
    assert state_of(hass, "sensor.api_example_com_v").state == "1"


async def test_stored_settings_that_do_not_fit_cannot_break_updates(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    subentry = ConfigSubentryData(
        data=mapping_data(
            "sensor", {"value": "$.d"}, {"value_type": "text", "device_class": "power_factor"}, {}
        ),
        subentry_type="sensor",
        title="D",
        unique_id=None,
    )
    await start(hass, make_entry(hass, hass_storage, {"d": "x"}, [subentry]))
    assert state_of(hass, "sensor.api_example_com_d").state == "unknown"


async def test_reconfigure_to_another_entry(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    await finish(hass, await begin(hass, "https://taken.example.com/"))
    calls = api.calls
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://taken.example.com/"}
    )
    assert result["errors"] == {"base": "already_configured"}
    assert api.calls == calls
