"""Setup and mapping flows: ha/endpoint-setup, ha/field-entities, ha/attribute-mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from custom_components.rest_mapper.const import DOMAIN
from custom_components.rest_mapper.lib import (
    AuthFailedError,
    ConnectFailedError,
    FormatError,
    JsonValue,
    RateLimitedError,
)
from custom_components.rest_mapper.mappings import mapping_data
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigFlowResult,
    ConfigSubentryData,
    SubentryFlowResult,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from conftest import (
    URL,
    FakeApi,
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
POLLING = {
    "mode": "automatic",
    "interval_seconds": 900,
    "budget_limit": 0,
    "budget_period": "month",
}


async def begin(hass: HomeAssistant, url: str = URL, auth_type: str = "none") -> ConfigFlowResult:
    """Start setup and submit the first step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": url, "auth_type": auth_type}
    )


async def test_full_setup(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    api.will(response(BODY, etag='"a"'))
    result = await begin(hass)
    assert result["step_id"] == "polling"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], POLLING)
    assert result["step_id"] == "cost"
    assert placeholders(result)["per_day"] == "96"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "fields"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_fields": ["$.current.temp_c", "$.current.open"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert api.calls == 1
    entry = result["result"]
    assert len(entry.subentries) == 2
    assert {item.subentry_type for item in entry.subentries.values()} == {
        "sensor",
        "binary_sensor",
    }
    temperature = state_of(hass, "sensor.api_example_com_temp_c")
    assert temperature.state == "21.5"
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert state_of(hass, "binary_sensor.api_example_com_open").state == "on"


async def test_cost_of_a_short_interval(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**POLLING, "interval_seconds": 30}
    )
    assert placeholders(result)["per_day"] == "2880"
    assert placeholders(result)["per_month"] == "86400"


async def test_budget_slower_than_the_interval(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**POLLING, "interval_seconds": 30, "budget_limit": 100}
    )
    assert placeholders(result)["budget_note"].isdigit()


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ConnectFailedError("x"), "cannot_connect"),
        (AuthFailedError(401), "invalid_auth"),
        (RateLimitedError(None), "rate_limited"),
        (FormatError("x"), "not_json"),
    ],
)
async def test_failed_test_request(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    failure: Exception,
    key: str,
) -> None:
    api.will(failure)  # type: ignore[arg-type]  # All these are fetch errors.
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
    result = await begin(hass, "http://api.example.com/", "bearer")
    assert result["step_id"] == "auth"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_value": "abc"}
    )
    assert result["step_id"] == "insecure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "polling"


async def setup_through_flow(hass: HomeAssistant, url: str = URL) -> ConfigFlowResult:
    """Finish setup with no fields."""
    result = await begin(hass, url)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], POLLING)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def test_same_endpoint_twice(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    await setup_through_flow(hass)
    await hass.async_block_till_done()
    result = await begin(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_same_url_with_another_key(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    api.will(response(BODY))
    await setup_through_flow(hass)
    await hass.async_block_till_done()
    result = await begin(hass, URL, "bearer")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_value": "other"}
    )
    assert result["step_id"] == "polling"


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
            **POLLING,
            "mode": "on_demand",
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


async def test_add_sensor_with_attributes(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"title": "Temp", "value": "$.current.temp_c"})
    assert result["step_id"] == "settings"
    result = await step(
        hass,
        result,
        {
            "value_type": "number",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
        },
    )
    assert result["step_id"] == "attributes"
    result = await step(hass, result, {"attributes": [{"path": "$.name"}]})
    assert result["step_id"] == "confirm"
    assert placeholders(result)["preview"] == "21.5 °C"
    result = await step(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert api.calls == 0
    state = state_of(hass, "sensor.api_example_com_temp")
    assert state.state == "21.5"
    assert state.attributes["name"] == "x"


async def test_invalid_path(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"title": "T", "value": "$.items[*]"})
    assert result["errors"] == {"base": "invalid_path"}


async def test_unit_does_not_fit(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"title": "T", "value": "$.current.temp_c"})
    result = await step(
        hass,
        result,
        {"value_type": "number", "device_class": "temperature", "unit_of_measurement": "kg"},
    )
    assert result["errors"] == {"base": "unit_mismatch"}


async def test_type_problem_shows_and_goes_back(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"title": "T", "value": "$.name"})
    result = await step(hass, result, {"value_type": "number"})
    result = await step(hass, result, {"attributes": []})
    assert "type_mismatch" in placeholders(result)["preview"]
    result = await step(hass, result, {"back": True})
    assert result["step_id"] == "user"


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
    result = await step(hass, result, {"title": "T", "value": "$.name"})
    result = await step(hass, result, {"value_type": "text"})
    result = await step(hass, result, {"attributes": rows})
    assert result["errors"] == {"base": key}


async def test_binary_sensor_with_own_values(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, {"door": "closed"}, [])
    await start(hass, entry)
    result = await add(hass, entry, "binary_sensor")
    result = await step(hass, result, {"title": "Door", "value": "$.door"})
    result = await step(hass, result, {"on_values": "open", "off_values": "closed"})
    result = await step(hass, result, {"attributes": []})
    result = await step(hass, result, {})
    await hass.async_block_till_done()
    assert state_of(hass, "binary_sensor.api_example_com_door").state == "off"


async def test_tracker(hass: HomeAssistant, hass_storage: Storage, api: FakeApi) -> None:
    hass.config.latitude = 50.26
    hass.config.longitude = 19.02
    body: JsonValue = {"v": {"lat": 50.26, "lon": 19.02}}
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    await hass.async_block_till_done()
    result = await add(hass, entry, "device_tracker")
    result = await step(
        hass, result, {"title": "Car", "latitude": "$.v.lat", "longitude": "$.v.lon"}
    )
    result = await step(hass, result, {"attributes": []})
    assert placeholders(result)["preview"] == "50.26, 19.02"
    result = await step(hass, result, {})
    await hass.async_block_till_done()
    state = state_of(hass, "device_tracker.api_example_com_car")
    assert state.attributes["latitude"] == 50.26
    registry = er.async_get(hass)
    item = registry.async_get("device_tracker.api_example_com_car")
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
    result = await step(hass, result, {"title": "T", "value": "$.name", "refresh_sample": True})
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


async def test_edit_keeps_the_entity_id(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    entry = make_entry(hass, hass_storage, BODY, [sensor_subentry("$.current.temp_c")])
    await start(hass, entry)
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "user"
    result = await step(hass, result, {"title": "Temp", "value": "$.current.open"})
    result = await step(hass, result, {"value_type": "text"})
    result = await step(hass, result, {"attributes": []})
    result = await step(hass, result, {})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.api_example_com_temp").state == "true"


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
    result = await step(hass, result, {"title": "T", "value": "$.name"})
    result = await step(hass, result, settings)
    assert result["errors"] == {"base": "value_type_mismatch"}


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
    await setup_through_flow(hass, "https://taken.example.com/")
    await hass.async_block_till_done()
    calls = api.calls
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "https://taken.example.com/"}
    )
    assert result["errors"] == {"base": "already_configured"}
    assert api.calls == calls


async def test_confirmation_lists_attributes_left_out(
    hass: HomeAssistant, hass_storage: Storage, api: FakeApi
) -> None:
    body: JsonValue = {"v": 1, "big": ["x" * 100] * 50}
    entry = make_entry(hass, hass_storage, body, [])
    await start(hass, entry)
    result = await add(hass, entry, "sensor")
    result = await step(hass, result, {"title": "T", "value": "$.v"})
    result = await step(hass, result, {"value_type": "number"})
    result = await step(hass, result, {"attributes": [{"path": "$.gone"}, {"path": "$.big"}]})
    preview = placeholders(result)["preview"]
    assert "Attribute missing: gone" in preview
    assert "Attribute too large: big" in preview
