"""One endpoint's requests, stored data and mapped values.

A failed request is a normal outcome here, so the update method does not raise.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, final, override

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_RETENTION,
    DOMAIN,
    REFRESH_COOLDOWN_SECONDS,
    SAVE_DELAY_SECONDS,
    STARTUP_DELAY,
    STARTUP_VARIATION_SECONDS,
)
from .endpoint import build_policy_config, build_spec
from .evaluation import (
    CollectionRead,
    apply_to_collection,
    collection_from_data,
    confirm,
    copy_collection,
    is_collection,
    new_tracker,
    read_value,
)
from .issues import async_remove_issues, async_update_issues
from .lib import (
    AuthFailedError,
    Fetched,
    FetchError,
    JsonValue,
    ManualDecision,
    NotModified,
    RateLimitedError,
    Refusal,
    RequestPolicy,
    ServerFailedError,
    Status,
    decode_json,
    fetch,
    redact_url,
)
from .mappings import LoadedMappings, load_mappings
from .values import ValueRead

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.helpers.storage import Store

    from .kinds import MappingConfig
    from .store import StoredEndpoint

_LOGGER = logging.getLogger(__name__)

type RestMapperConfigEntry = ConfigEntry[EndpointCoordinator]


@final
@dataclass(slots=True)
class EndpointData:
    """Everything entities show."""

    reads: dict[str, ValueRead] = field(default_factory=dict)
    collections: dict[str, CollectionRead] = field(default_factory=dict)
    confirmed_at: datetime | None = None


def _evaluate(
    body: JsonValue,
    mappings: Mapping[str, MappingConfig],
    data: EndpointData,
    now: datetime,
    local: tzinfo,
) -> EndpointData:
    """Apply a new response. This runs in the executor."""
    result = EndpointData(dict(data.reads), dict(data.collections), now)
    for subentry_id, mapping in mappings.items():
        if is_collection(mapping):
            previous_collection = result.collections.get(subentry_id)
            collection = (
                copy_collection(mapping, previous_collection)
                if previous_collection is not None
                else CollectionRead(new_tracker(mapping))
            )
            result.collections[subentry_id] = collection
            apply_to_collection(body, mapping, collection, now, local)
        else:
            previous = result.reads.get(subentry_id, ValueRead())
            result.reads[subentry_id] = read_value(body, mapping, previous, now, local)
    return result


@final
class EndpointCoordinator(DataUpdateCoordinator[EndpointData]):
    """Fetches when the policy allows it, and always returns data."""

    config_entry: RestMapperConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RestMapperConfigEntry,
        store: Store[dict[str, JsonValue]],
        stored: StoredEndpoint,
    ) -> None:
        """Set up from stored data. No request is sent."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=redact_url(str(entry.data["url"])),
            update_interval=None,
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REFRESH_COOLDOWN_SECONDS, immediate=True
            ),
        )
        self._store = store
        self.stored = stored
        loaded: LoadedMappings = load_mappings(entry.subentries)
        self.mappings = loaded.mappings
        self.skipped = loaded.skipped
        self.policy = RequestPolicy(
            build_policy_config(entry.options), dt_util.utcnow, random.random, stored.policy
        )
        self.endpoint_device_id = ""
        self.platforms: list[Platform] = []
        self.last_failed = False
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._cancel_expiry: CALLBACK_TYPE | None = None
        self._automatic = False
        self._failing = False
        self._save_pending = False
        self._last_finished: datetime | None = None

    @property
    def retention(self) -> timedelta | None:
        """Return how long data stays usable."""
        seconds = self.config_entry.options.get(CONF_RETENTION, 0)
        return timedelta(seconds=seconds) if seconds else None

    @property
    def expired(self) -> bool:
        """Tell whether data is older than the retention limit."""
        confirmed = self.data.confirmed_at
        limit = self.retention
        if limit is None or confirmed is None:
            return False
        return dt_util.utcnow() - confirmed > limit

    @property
    def stale(self) -> bool:
        """Tell whether data is too old or the last automatic request failed."""
        return self.expired or self.last_failed

    async def async_start(self) -> None:
        """Rebuild values from storage, and schedule the next request."""
        stored = self.stored
        data = EndpointData(confirmed_at=stored.confirmed_at)
        fresh: dict[str, MappingConfig] = {}
        for subentry_id, mapping in self.mappings.items():
            if stored.fingerprints.get(subentry_id) != mapping.fingerprint:
                fresh[subentry_id] = mapping
            elif is_collection(mapping):
                data.collections[subentry_id] = collection_from_data(
                    mapping, stored.collections.get(subentry_id)
                )
            else:
                data.reads[subentry_id] = ValueRead.from_data(stored.reads.get(subentry_id))
        if fresh and stored.has_body:
            moment = stored.confirmed_at or dt_util.utcnow()
            updated = await self.hass.async_add_executor_job(
                _evaluate, stored.body, fresh, data, moment, dt_util.get_default_time_zone()
            )
            updated.confirmed_at = stored.confirmed_at
            data = updated
            for collection in data.collections.values():
                collection.added = ()
        self.data = data
        if self.policy.state.auth_stopped:
            self.config_entry.async_start_reauth(self.hass)
        self._schedule_next(startup=True)
        self._schedule_expiry(data.confirmed_at)
        async_remove_issues(self.hass, entry_id=self.config_entry.entry_id)
        async_update_issues(self)
        self._save_later()

    def _schedule_next(self, *, startup: bool = False) -> None:
        """Set the timer for the next automatic request."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        moment = self.policy.next_request_at()
        if moment is None or self._shutdown_requested:
            return
        if startup:
            earliest = dt_util.utcnow() + STARTUP_DELAY
            if moment < earliest:
                variation = random.uniform(0, STARTUP_VARIATION_SECONDS)
                moment = earliest + timedelta(seconds=variation)
        self._cancel_timer = async_track_point_in_utc_time(self.hass, self._on_timer, moment)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        """Run an automatic request."""
        self._cancel_timer = None
        if self._shutdown_requested:
            return
        self._automatic = True
        _ = self.config_entry.async_create_task(self.hass, self.async_refresh(), f"{DOMAIN} poll")

    def _schedule_expiry(self, confirmed: datetime | None) -> None:
        """Wake entities when data passes the retention limit."""
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
        limit = self.retention
        if limit is None or confirmed is None or self._shutdown_requested:
            return
        moment = confirmed + limit + timedelta(seconds=1)
        if moment > dt_util.utcnow():
            self._cancel_expiry = async_track_point_in_utc_time(self.hass, self._on_expiry, moment)

    @callback
    def _on_expiry(self, _now: datetime) -> None:
        """Tell entities that data has expired."""
        self._cancel_expiry = None
        self.async_update_listeners()

    @override
    async def _async_update_data(self) -> EndpointData:
        """Send a request if allowed, and return what is known."""
        automatic = self._automatic
        self._automatic = False
        try:
            if automatic and not self.policy.is_due():
                return self.data
            if not automatic:
                if self._just_fetched():
                    return self.data
                decision = self.policy.check_manual()
                if not decision.allowed:
                    _LOGGER.warning(
                        "Refresh of %s refused until %s: %s",
                        self.name,
                        decision.possible_at,
                        decision.refusal,
                    )
                    return self.data
            return await self._request(automatic=automatic)
        finally:
            self._schedule_next()

    def _just_fetched(self) -> bool:
        """Tell whether a refresh asked for during a request would only repeat it.

        Refreshes run one after another, so such a refresh arrives right after it ends.
        """
        finished = self._last_finished
        cooldown = timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
        return finished is not None and dt_util.utcnow() - finished < cooldown

    async def _request(self, *, automatic: bool) -> EndpointData:
        """Send one request and record its outcome."""
        try:
            return await self._send(automatic=automatic)
        finally:
            self._last_finished = dt_util.utcnow()

    async def _send(self, *, automatic: bool) -> EndpointData:
        """Fetch, and turn every outcome into data."""
        policy = self.policy
        policy.record_sent()
        self._save_later()
        session = async_get_clientsession(self.hass)
        try:
            outcome = await fetch(
                session,
                build_spec(self.config_entry.data, self.config_entry.options),
                policy.validators(),
            )
            result = await self._apply(outcome)
        except AuthFailedError as err:
            policy.record_auth_failed()
            self._note_failure(err, automatic=automatic)
            self.config_entry.async_start_reauth(self.hass)
            return self.data
        except RateLimitedError as err:
            policy.record_rate_limited(err.retry_after)
            self._note_failure(err, automatic=automatic)
            async_update_issues(self)
            return self.data
        except ServerFailedError as err:
            policy.record_failure(err.retry_after)
            self._note_failure(err, automatic=automatic)
            return self.data
        except FetchError as err:
            policy.record_failure()
            self._note_failure(err, automatic=automatic)
            return self.data
        self.last_failed = False
        if self._failing:
            self._failing = False
            _LOGGER.info("Requests to %s work again", self.name)
        self._schedule_expiry(result.confirmed_at)
        async_update_issues(self, result)
        return result

    def _note_failure(self, err: FetchError, *, automatic: bool) -> None:
        """Log a failure once, and mark data as doubtful."""
        if automatic:
            self.last_failed = True
        if not self._failing:
            self._failing = True
            _LOGGER.warning("Request to %s failed: %s", self.name, err)

    async def _apply(self, outcome: Fetched | NotModified) -> EndpointData:
        """Turn a request's outcome into data."""
        policy = self.policy
        now = dt_util.utcnow()
        if isinstance(outcome, NotModified):
            if not self.stored.has_body:
                policy.discard_validators()
                return self.data
            policy.record_not_modified()
            return self._confirm_all(now)
        body = await self.hass.async_add_executor_job(decode_json, outcome.body)
        policy.record_fetched(outcome.etag, outcome.last_modified)
        self.stored.body = body
        self.stored.has_body = True
        return await self.hass.async_add_executor_job(
            _evaluate, body, self.mappings, self.data, now, dt_util.get_default_time_zone()
        )

    def _confirm_all(self, now: datetime) -> EndpointData:
        """Note that a `304` confirmed everything still readable."""
        data = self.data
        for read in data.reads.values():
            confirm(read, now)
        for collection in data.collections.values():
            collection.added = ()
            for read in collection.items.values():
                confirm(read, now)
        data.confirmed_at = now
        return data

    def refusal_error(self, decision: ManualDecision) -> HomeAssistantError:
        """Describe a refused manual request."""
        possible_at = decision.possible_at
        key = "budget_exhausted" if decision.refusal is Refusal.BUDGET_EXHAUSTED else "rate_limited"
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={
                "time": dt_util.as_local(possible_at).strftime("%Y-%m-%d %H:%M")
                if possible_at
                else "?"
            },
        )

    async def async_manual_refresh(self) -> None:
        """Run a refresh asked for by the user, or explain why it cannot run."""
        decision = self.policy.check_manual()
        if not decision.allowed:
            raise self.refusal_error(decision)
        await self.async_request_refresh()

    async def async_fetch_sample(self) -> None:
        """Fetch fresh data for a mapping flow."""
        decision = self.policy.check_manual()
        if not decision.allowed:
            raise self.refusal_error(decision)
        await self.async_refresh()

    async def async_replace_sample(
        self, fetched: Fetched, *, credentials_changed: bool, address_changed: bool = False
    ) -> None:
        """Use a test request's result from reconfiguring or reauthenticating."""
        policy = self.policy
        if address_changed:
            self.data = EndpointData(self.data.reads, {}, self.data.confirmed_at)
        policy.record_sent()
        if credentials_changed:
            policy.credentials_changed()
        data = await self._apply(fetched)
        self.last_failed = False
        self._failing = False
        self.async_set_updated_data(data)
        self._schedule_next()
        self._schedule_expiry(data.confirmed_at)
        async_update_issues(self, data)
        await self.async_save()

    def status(self) -> Status:
        """Return the endpoint's status."""
        return self.policy.status()

    def _snapshot(self) -> dict[str, JsonValue]:
        """Collect what is stored."""
        self._save_pending = False
        data = self.data
        stored = self.stored
        stored.policy = self.policy.state
        stored.confirmed_at = data.confirmed_at
        stored.reads = {key: read.to_data() for key, read in data.reads.items()}
        stored.collections = {key: item.to_data() for key, item in data.collections.items()}
        stored.fingerprints = {key: item.fingerprint for key, item in self.mappings.items()}
        return stored.to_data()

    def _save_later(self) -> None:
        """Save after a delay, so frequent polling writes rarely.

        A second call would push the pending write back, and short intervals
        would then postpone it for good.
        """
        if not self._save_pending and not self._shutdown_requested:
            self._save_pending = True
            self._store.async_delay_save(self._snapshot, SAVE_DELAY_SECONDS)

    async def async_save(self) -> None:
        """Save now."""
        await self._store.async_save(self._snapshot())

    @override
    async def async_shutdown(self) -> None:
        """Stop timers."""
        await super().async_shutdown()
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
