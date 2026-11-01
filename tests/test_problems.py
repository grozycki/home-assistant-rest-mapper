"""Repairs, diagnostics and secrets: ha/problem-reporting."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from custom_components.rest_mapper.const import DOMAIN
from custom_components.rest_mapper.diagnostics import async_get_config_entry_diagnostics
from custom_components.rest_mapper.lib import ConnectFailedError, RateLimitedError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import URL, FakeApi, make_entry, response, sensor_subentry, start
from test_collections import SENSORS, collection, items

if TYPE_CHECKING:
    import pytest
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

Storage = dict[str, object]


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, hours: float = 1) -> None:
    """Move past the next request."""
    freezer.tick(timedelta(hours=hours))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def issue_keys(hass: HomeAssistant) -> set[str | None]:
    """List raised issues."""
    return {
        issue.translation_key
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


async def test_broken_mapping(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, {"temp": 1}, [sensor_subentry()]))
    api.will(response({"other": 1}))
    await poll(hass, freezer)
    await poll(hass, freezer)
    assert issue_keys(hass) == set()
    await poll(hass, freezer)
    assert issue_keys(hass) == {"broken_mapping"}
    api.will(response({"temp": 2}))
    await poll(hass, freezer)
    assert issue_keys(hass) == set()


async def test_failures_do_not_count_as_broken(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, {"temp": 1}, [sensor_subentry()]))
    api.will(ConnectFailedError("down"))
    for _ in range(5):
        await poll(hass, freezer, 7)
    assert issue_keys(hass) == set()


async def test_removed_mapping_clears_its_issue(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = make_entry(hass, hass_storage, {"temp": 1}, [sensor_subentry()])
    await start(hass, entry)
    api.will(response({"other": 1}))
    for _ in range(3):
        await poll(hass, freezer)
    hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    await hass.async_block_till_done()
    assert issue_keys(hass) == set()


async def test_list_missing(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, items(1), [SENSORS]))
    api.will(response({"nothing": 1}))
    for _ in range(3):
        await poll(hass, freezer)
    assert issue_keys(hass) == {"collection_list_missing"}


async def test_limit_reached(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", item_limit=1)
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    api.will(response(items(1, 2)))
    await poll(hass, freezer)
    assert issue_keys(hass) == {"collection_limit"}


async def test_wrong_key(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = collection("sensor", {"value": "v"}, value_type="number", key="nope")
    await start(hass, make_entry(hass, hass_storage, items(1), [entry]))
    api.will(response(items(1, 2, 3)))
    await poll(hass, freezer)
    assert issue_keys(hass) == {"collection_unreadable"}


async def test_rate_limit_repair(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, make_entry(hass, hass_storage, {"temp": 1}, [sensor_subentry()]))
    api.will(RateLimitedError(None))
    for _ in range(3):
        await poll(hass, freezer, 7)
    assert issue_keys(hass) == {"rate_limited"}
    api.will(response({"temp": 1}))
    await poll(hass, freezer, 7)
    assert issue_keys(hass) == set()


async def test_diagnostics_hold_no_secrets(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.will(response({"temp": 22, "name": "private value"}, etag='"etag-marker"'))
    entry = make_entry(
        hass, hass_storage, {"temp": 21.5, "name": "private value"}, [sensor_subentry()]
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "auth_type": "header",
            "auth_name": "X-Key",
            "auth_value": "header-secret",
        },
    )
    await start(hass, entry)
    await poll(hass, freezer)
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    assert URL not in text
    for secret in ("secret", "etag-marker", "private value"):
        assert secret not in text
    assert "X-Key" in text
    assert "api.example.com/data" in text
    assert "$.temp" in text


async def test_logs_hold_no_secrets(
    hass: HomeAssistant,
    hass_storage: Storage,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api.will(ConnectFailedError("down for https://api.example.com/data"))
    await start(hass, make_entry(hass, hass_storage, {"temp": 1}, [sensor_subentry()]))
    for _ in range(3):
        await poll(hass, freezer, 7)
    assert "key=secret" not in caplog.text
    warnings = [record for record in caplog.records if "failed" in record.getMessage()]
    assert len([record for record in warnings if record.levelno == logging.WARNING]) == 1
