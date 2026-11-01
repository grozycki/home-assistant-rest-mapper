"""Setting up endpoints and mapping their values.

Choosing paths uses stored data, so these flows send no request unless asked to.
"""

from __future__ import annotations

import hashlib
import ipaddress
import random
import re
import secrets
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final, final, override

import probatio as vol
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    DEVICE_CLASS_STATE_CLASSES,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    ObjectSelectorField,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from yarl import URL

from .const import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    AUTH_QUERY,
    COLLECTION,
    CONF_AUTH_NAME,
    CONF_AUTH_TYPE,
    CONF_AUTH_VALUE,
    CONF_BUDGET_LIMIT,
    CONF_BUDGET_PERIOD,
    CONF_INTERVAL,
    CONF_MAX_KIB,
    CONF_MODE,
    CONF_RESERVE,
    CONF_RETENTION,
    CONF_STORAGE_ID,
    CONF_TIMEOUT,
    CONF_URL,
    DOMAIN,
    MAX_ATTRIBUTES,
    MIN_RETENTION,
    RETENTION_INTERVALS,
    ROLE_NAME,
)
from .endpoint import build_policy_config, build_spec
from .kinds import KINDS, KindDefinition, Shape
from .lib import (
    DEFAULT_MAX_BYTES,
    DEFAULT_RESERVE_PERCENT,
    DEFAULT_TIMEOUT,
    MAX_ITEM_LIMIT,
    MAX_MAX_BYTES,
    MIN_INTERVAL,
    MIN_MAX_BYTES,
    AuthFailedError,
    BudgetPeriod,
    ClientFailedError,
    CollectionCandidate,
    CollectionSuggestion,
    ConnectFailedError,
    Failure,
    Fetched,
    FetchError,
    FieldSuggestion,
    FlatField,
    Flattened,
    FormatError,
    JsonValue,
    RateLimitedError,
    RedirectError,
    RequestPolicy,
    ScalarType,
    ServerFailedError,
    SizeExceededError,
    UpdateMode,
    ValueType,
    decode_json,
    fetch,
    find_url_key,
    flatten,
    label_for,
    name_for,
    parse_path,
    suggest_collection,
    suggest_field,
)
from .lib import PathError as _PathError
from .mappings import mapping_data, parse_mapping, source_for
from .proposals import (
    Check,
    Proposal,
    build_proposals,
    check_collection,
    check_single,
    collection_settings,
    collection_title,
    field_data,
    roles_for,
)
from .store import StoredEndpoint, async_load, endpoint_store
from .texts import Texts, async_load_texts

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .coordinator import EndpointCoordinator

ATTRIBUTES: Final = "attributes"
TITLE: Final = "title"
REFRESH: Final = "refresh_sample"
ADVANCED: Final = "advanced"
SAVE_ANYWAY: Final = "save_anyway"
LIST_PATH: Final = "list_path"
KEY_PATH: Final = "key_path"
KIND: Final = "kind"
ITEM_LIMIT: Final = "item_limit"
REMOVAL_AFTER: Final = "removal_after"
EVENTS: Final = "events"
SELECTED: Final = "selected_fields"
_DAY_SECONDS: Final = 86400
_MONTH_DAYS: Final = 30
_MAX_INTERVAL_SECONDS: Final = 86400
_NAME_PATTERN: Final = re.compile(r"[a-z0-9_]+")
_RESERVED_NAMES: Final = frozenset(
    {
        "latitude",
        "longitude",
        "gps_accuracy",
        "source_type",
        "friendly_name",
        "unit_of_measurement",
        "device_class",
        "state_class",
        "icon",
        "last_confirmed",
        "entity_picture",
        "assumed_state",
        "supported_features",
    }
)
_PRESET: Final = "polling"
_PRESETS: Final = (300, 900, 3600, 86400)
_DEFAULT_PRESET: Final = "900"
_ON_DEMAND: Final = "on_demand"
_TIME_CLASSES: Final = (SensorDeviceClass.TIMESTAMP, SensorDeviceClass.DATE)


def _text_field(*, password: bool = False) -> TextSelector:
    """Return a text field."""
    kind = TextSelectorType.PASSWORD if password else TextSelectorType.TEXT
    return TextSelector(TextSelectorConfig(type=kind))


def _number_field(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    """Return a number box."""
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    )


def _choice(values: Sequence[str], translation_key: str) -> SelectSelector:
    """Return a translated choice."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(values),
            translation_key=translation_key,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _plain_choice(values: Sequence[str], *, custom: bool = False) -> SelectSelector:
    """Return a choice of values shown as they are."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(values), custom_value=custom, mode=SelectSelectorMode.DROPDOWN
        )
    )


def _is_public_host(host: str | None) -> bool:
    """Tell whether a host seems to be outside the local network."""
    if host is None or host == "localhost" or host.endswith((".local", ".lan", ".home.arpa")):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return "." in host


def _validate_url(text: str) -> str | None:
    """Return an error key for a URL."""
    try:
        url = URL(text)
    except ValueError:
        return "invalid_url"
    if url.scheme not in ("http", "https") or not url.host:
        return "invalid_url"
    if url.user or url.password:
        return "credentials_in_url"
    return None


def _unique_id(data: Mapping[str, Any]) -> str:
    """Identify an endpoint with no secret readable from the result."""
    parts = (CONF_URL, CONF_AUTH_TYPE, CONF_AUTH_NAME, CONF_AUTH_VALUE)
    text = "\n".join(str(data.get(part, "")) for part in parts)
    return hashlib.sha256(text.encode()).hexdigest()


@final
@dataclass(slots=True)
class _TestResult:
    """A successful test request."""

    fetched: Fetched
    body: JsonValue
    found: Flattened


def _error_key(err: FetchError) -> str:
    """Name the message for a failed test."""
    match err:
        case AuthFailedError():
            return "invalid_auth"
        case RateLimitedError():
            return "rate_limited"
        case ServerFailedError():
            return "server_error"
        case ClientFailedError():
            return "bad_status"
        case RedirectError():
            return "bad_redirect"
        case SizeExceededError():
            return "too_large"
        case FormatError():
            return "not_json"
        case ConnectFailedError():
            return "cannot_connect"
        case _:
            return "cannot_connect"


def _decode_and_flatten(raw: bytes) -> tuple[JsonValue, Flattened]:
    """Decode and list a body. This runs in the executor."""
    body = decode_json(raw)
    return body, flatten(body)


def _options_defaults(options: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in default options."""
    return {
        CONF_MODE: UpdateMode.AUTOMATIC.value,
        CONF_INTERVAL: 900,
        CONF_BUDGET_LIMIT: 0,
        CONF_BUDGET_PERIOD: BudgetPeriod.MONTH.value,
        CONF_RESERVE: DEFAULT_RESERVE_PERCENT,
        CONF_MAX_KIB: DEFAULT_MAX_BYTES // 1024,
        CONF_TIMEOUT: int(DEFAULT_TIMEOUT),
        **options,
    }


def _suggested_retention(options: Mapping[str, Any]) -> int:
    """Suggest the longer of 1 hour and 4 intervals, or none on demand."""
    if options.get(CONF_MODE) == UpdateMode.ON_DEMAND:
        return 0
    interval = int(options.get(CONF_INTERVAL, 900))
    return max(int(MIN_RETENTION.total_seconds()), RETENTION_INTERVALS * interval)


def _polling_schema() -> dict[vol.Marker, Any]:
    """Return polling fields."""
    return {
        vol.Required(CONF_MODE): _choice(tuple(UpdateMode), "mode"),
        vol.Required(CONF_INTERVAL): _number_field(
            MIN_INTERVAL.total_seconds(), _MAX_INTERVAL_SECONDS
        ),
        vol.Required(CONF_BUDGET_LIMIT): _number_field(0, 100_000_000),
        vol.Required(CONF_BUDGET_PERIOD): _choice(tuple(BudgetPeriod), "budget_period"),
    }


def _preset_cost(interval: int) -> int:
    """Return requests a month for an interval."""
    return _DAY_SECONDS * _MONTH_DAYS // interval


def _preset_field(texts: Texts) -> SelectSelector:
    """Return the polling choice, with what each option costs."""
    options = [
        SelectOptionDict(
            value=str(interval),
            label=texts(f"poll_{interval}", requests=_preset_cost(interval)),
        )
        for interval in _PRESETS
    ]
    options.append(SelectOptionDict(value=_ON_DEMAND, label=texts("poll_on_demand")))
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))


def _polling_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the found screen's answers into options."""
    advanced = user_input.get(ADVANCED, {})
    preset = user_input.get(_PRESET, _DEFAULT_PRESET)
    custom = advanced.get(CONF_INTERVAL)
    on_demand = preset == _ON_DEMAND
    options = _options_defaults(
        {
            CONF_MODE: UpdateMode.ON_DEMAND.value if on_demand else UpdateMode.AUTOMATIC.value,
            CONF_INTERVAL: custom or (int(_DEFAULT_PRESET) if on_demand else int(preset)),
            CONF_BUDGET_LIMIT: user_input.get(CONF_BUDGET_LIMIT) or 0,
            CONF_BUDGET_PERIOD: advanced.get(CONF_BUDGET_PERIOD, BudgetPeriod.MONTH.value),
        }
    )
    options[CONF_RETENTION] = _suggested_retention(options)
    return options


def _suggest_lists(
    body: JsonValue, found: Flattened
) -> list[tuple[CollectionCandidate, CollectionSuggestion]]:
    """Propose a mapping for each list. This runs in the executor."""
    now = dt_util.utcnow()
    zone = dt_util.get_default_time_zone()
    return [
        (candidate, suggest_collection(body, candidate, now, zone))
        for candidate in found.collections
    ]


def _list_data(
    body: JsonValue,
) -> tuple[Flattened, list[tuple[CollectionCandidate, CollectionSuggestion]]]:
    """List a body with a proposal for each list. This runs in the executor."""
    found = flatten(body)
    return found, _suggest_lists(body, found)


class RestMapperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adds an endpoint: a URL, then a screen of what was found."""

    VERSION = 1

    def __init__(self) -> None:
        """Start empty."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._result: _TestResult | None = None
        self._confirmed_insecure = False

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return RestMapperOptionsFlow()

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Offer each available kind."""
        types: dict[str, type[ConfigSubentryFlow]] = {
            name: _single_flow(name)
            for name, definition in KINDS.items()
            if Shape.SINGLE in definition.shapes and definition.available()
        }
        types[COLLECTION] = CollectionFlow
        return types

    def _user_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Ask for the URL."""
        advanced = vol.Schema(
            {
                vol.Optional(CONF_AUTH_TYPE): _choice(
                    (AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_QUERY), "auth_type"
                )
            }
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): _text_field(),
                vol.Optional(ADVANCED): section(advanced, {"collapsed": True}),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_URL: self._data.get(CONF_URL, "")}
            ),
            errors=errors,
        )

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take a URL, moving any key in it to the credentials."""
        if user_input is None:
            return self._user_form({})
        url = str(user_input[CONF_URL]).strip()
        self._data = {CONF_URL: url, CONF_AUTH_TYPE: AUTH_NONE}
        self._confirmed_insecure = False
        error = _validate_url(url)
        if error is not None:
            return self._user_form({CONF_URL: error})
        pasted = find_url_key(url)
        if pasted is not None:
            self._data = {
                CONF_URL: pasted.url,
                CONF_AUTH_TYPE: AUTH_QUERY,
                CONF_AUTH_NAME: pasted.name,
                CONF_AUTH_VALUE: pasted.value,
            }
        elif user_input.get(ADVANCED, {}).get(CONF_AUTH_TYPE, AUTH_NONE) != AUTH_NONE:
            self._data[CONF_AUTH_TYPE] = user_input[ADVANCED][CONF_AUTH_TYPE]
            return await self.async_step_auth()
        return await self._async_test()

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for credentials, only when they are needed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            auth_type = user_input[CONF_AUTH_TYPE]
            name = str(user_input.get(CONF_AUTH_NAME, "")).strip()
            if auth_type != AUTH_BEARER and not name:
                errors[CONF_AUTH_NAME] = "name_required"
            else:
                self._data.update(
                    {
                        CONF_AUTH_TYPE: auth_type,
                        CONF_AUTH_NAME: name,
                        CONF_AUTH_VALUE: user_input[CONF_AUTH_VALUE],
                    }
                )
                return await self._async_test()
        return self._auth_form(errors)

    def _auth_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Ask for credentials."""
        current = self._data.get(CONF_AUTH_TYPE, AUTH_NONE)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_AUTH_TYPE, default=AUTH_BEARER if current == AUTH_NONE else current
                ): _choice((AUTH_BEARER, AUTH_HEADER, AUTH_QUERY), "auth_type"),
                vol.Required(CONF_AUTH_VALUE): _text_field(password=True),
                vol.Optional(CONF_AUTH_NAME): _text_field(),
            }
        )
        return self.async_show_form(step_id="auth", data_schema=schema, errors=errors)

    async def async_step_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn about credentials on plain HTTP."""
        if user_input is not None:
            self._confirmed_insecure = True
            return await self._async_test()
        return self.async_show_form(step_id="insecure", data_schema=vol.Schema({}))

    def _needs_insecure_warning(self) -> bool:
        """Tell whether credentials would cross the internet unencrypted."""
        url = URL(self._data[CONF_URL])
        return (
            self._data.get(CONF_AUTH_TYPE) != AUTH_NONE
            and not self._confirmed_insecure
            and url.scheme == "http"
            and _is_public_host(url.host)
        )

    async def _async_run_test(self, data: Mapping[str, Any]) -> str | None:
        """Send the test request, and return an error key."""
        spec = build_spec(data, self._options)
        session = async_get_clientsession(self.hass)
        try:
            outcome = await fetch(session, spec)
            if not isinstance(outcome, Fetched):
                return "bad_status"
            body, found = await self.hass.async_add_executor_job(_decode_and_flatten, outcome.body)
        except FetchError as err:
            return _error_key(err)
        self._result = _TestResult(outcome, body, found)
        return None

    async def _async_test(self) -> ConfigFlowResult:
        """Test a new endpoint."""
        if self._needs_insecure_warning():
            return await self.async_step_insecure()
        await self.async_set_unique_id(_unique_id(self._data))
        self._abort_if_unique_id_configured()
        error = await self._async_run_test(self._data)
        if error == "invalid_auth":
            refused = self._data[CONF_AUTH_TYPE] != AUTH_NONE
            return self._auth_form({"base": error} if refused else {})
        if error is not None:
            return self._user_form({"base": error})
        return await self.async_step_found()

    async def async_step_found(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show what the response holds, and how often to ask for it."""
        result = self._result
        if result is None:
            return self.async_abort(reason="unknown")
        texts = await async_load_texts(self.hass)
        host = URL(self._data[CONF_URL]).host or "REST Mapper"
        lists = await self.hass.async_add_executor_job(_suggest_lists, result.body, result.found)
        proposals = build_proposals(lists, result.found.fields, host.partition(".")[0], texts)
        if user_input is not None:
            chosen = set(user_input.get(SELECTED, []))
            return await self._async_finish(
                result,
                [item for item in proposals if item.identifier in chosen],
                _polling_options(user_input),
            )
        advanced = vol.Schema(
            {
                vol.Optional(CONF_INTERVAL): _number_field(
                    MIN_INTERVAL.total_seconds(), _MAX_INTERVAL_SECONDS
                ),
                vol.Optional(CONF_BUDGET_PERIOD): _choice(tuple(BudgetPeriod), "budget_period"),
            }
        )
        fields: dict[vol.Marker, Any] = {}
        if proposals:
            fields[
                vol.Optional(
                    SELECTED,
                    default=[item.identifier for item in proposals if item.preselected],
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=item.identifier, label=item.label)
                        for item in proposals
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        fields[vol.Required(_PRESET, default=_DEFAULT_PRESET)] = _preset_field(texts)
        fields[vol.Optional(CONF_BUDGET_LIMIT)] = _number_field(0, 100_000_000)
        fields[vol.Optional(ADVANCED)] = section(advanced, {"collapsed": True})
        return self.async_show_form(
            step_id="found",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "note": "" if proposals else texts("found_nothing"),
            },
        )

    async def _async_finish(
        self,
        result: _TestResult,
        chosen: Sequence[Proposal],
        options: dict[str, Any],
    ) -> ConfigFlowResult:
        """Store the sample, and create the entry with its mappings."""
        storage_id = secrets.token_hex(8)
        policy = RequestPolicy(build_policy_config(options), dt_util.utcnow, random.random)
        policy.record_sent()
        policy.record_fetched(result.fetched.etag, result.fetched.last_modified)
        stored = StoredEndpoint(
            body=result.body,
            has_body=True,
            policy=policy.state,
            confirmed_at=dt_util.utcnow(),
        )
        await endpoint_store(self.hass, storage_id).async_save(stored.to_data())
        subentries = [
            ConfigSubentryData(
                data=item.data,
                subentry_type=item.subentry_type,
                title=item.title,
                unique_id=None,
            )
            for item in chosen
        ]
        return self.async_create_entry(
            title=URL(self._data[CONF_URL]).host or "REST Mapper",
            data={**self._data, CONF_STORAGE_ID: storage_id},
            options=options,
            subentries=subentries,
        )

    def _auth_schema(self, auth_type: str) -> vol.Schema:
        """Return credential fields."""
        fields: dict[vol.Marker, Any] = {}
        if auth_type in (AUTH_HEADER, AUTH_QUERY):
            fields[vol.Required(CONF_AUTH_NAME)] = _text_field()
        fields[vol.Required(CONF_AUTH_VALUE)] = _text_field(password=True)
        return vol.Schema(fields)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start asking for new credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials and test them."""
        entry = self._get_reauth_entry()
        auth_type = entry.data[CONF_AUTH_TYPE]
        schema = vol.Schema({}) if auth_type == AUTH_NONE else self._auth_schema(auth_type)
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            self._options = dict(entry.options)
            error = await self._async_run_test(data)
            if error is None:
                await self._async_apply(entry, data)
                return self.async_abort(reason="reauth_successful")
            errors["base"] = error
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL and credentials, with one test request."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **{k: v for k, v in user_input.items() if v}}
            error = _validate_url(user_input[CONF_URL])
            other = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, _unique_id(data)
            )
            if error is not None:
                errors[CONF_URL] = error
            elif other is not None and other.entry_id != entry.entry_id:
                errors["base"] = "already_configured"
            else:
                self._options = dict(entry.options)
                error = await self._async_run_test(data)
                if error is None:
                    await self._async_apply(entry, data)
                    return self.async_abort(reason="reconfigure_successful")
                errors["base"] = error
        fields: dict[vol.Marker, Any] = {vol.Required(CONF_URL): _text_field()}
        if entry.data[CONF_AUTH_TYPE] != AUTH_NONE:
            # Left empty, the stored key stays, so changing an address needs no key.
            fields[vol.Optional(CONF_AUTH_VALUE)] = _text_field(password=True)
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), {CONF_URL: entry.data[CONF_URL]}
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def _async_apply(self, entry: ConfigEntry, data: dict[str, Any]) -> None:
        """Replace the sample and settings. The update listener then reloads."""
        result = self._result
        if result is None:
            return
        if entry.state is ConfigEntryState.LOADED:
            coordinator: EndpointCoordinator = entry.runtime_data
            await coordinator.async_replace_sample(result.fetched, credentials_changed=True)
        else:
            store = endpoint_store(self.hass, str(data[CONF_STORAGE_ID]))
            stored = await async_load(store)
            policy = RequestPolicy(
                build_policy_config(entry.options), dt_util.utcnow, random.random, stored.policy
            )
            policy.record_sent()
            policy.credentials_changed()
            policy.record_fetched(result.fetched.etag, result.fetched.last_modified)
            stored.body = result.body
            stored.has_body = True
            stored.confirmed_at = dt_util.utcnow()
            await store.async_save(stored.to_data())
        self.hass.config_entries.async_update_entry(entry, data=data, unique_id=_unique_id(data))


class RestMapperOptionsFlow(OptionsFlow):
    """Changes polling and limits. Applying them sends no request."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        fields = _polling_schema()
        fields[vol.Required(CONF_RESERVE)] = _number_field(0, 50)
        fields[vol.Required(CONF_RETENTION)] = _number_field(0, 31_536_000)
        fields[vol.Required(CONF_MAX_KIB)] = _number_field(
            MIN_MAX_BYTES // 1024, MAX_MAX_BYTES // 1024
        )
        fields[vol.Required(CONF_TIMEOUT)] = _number_field(5, 60)
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), _options_defaults(self.config_entry.options)
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _attribute_field(paths: Sequence[FlatField]) -> ObjectSelector:
    """Return the editor for a list of attributes."""
    return ObjectSelector(
        ObjectSelectorConfig(
            multiple=True,
            label_field="path",
            translation_key="attribute",
            fields={
                "name": ObjectSelectorField(required=False, selector={"text": {}}),
                "path": ObjectSelectorField(
                    required=True,
                    selector={
                        "select": {
                            "options": [
                                {"value": item.path, "label": _label(item)} for item in paths
                            ],
                            "custom_value": True,
                        }
                    },
                ),
            },
        )
    )


def _label(item: FlatField) -> str:
    """Show a field by a readable name and a real example."""
    return f"{label_for(item.path)}: {item.example}"


def _read_attribute_rows(
    rows: Sequence[Mapping[str, str]], *, relative: bool
) -> tuple[dict[str, str], str | None]:
    """Turn editor rows into names and paths, or an error key."""
    attributes: dict[str, str] = {}
    if len(rows) > MAX_ATTRIBUTES:
        return {}, "too_many_attributes"
    for row in rows:
        path = row.get("path", "").strip()
        try:
            parse_path(path, relative=relative)
        except _PathError:
            return {}, "invalid_path"
        name = row.get("name", "").strip() or slugify(name_for(path))
        if not _NAME_PATTERN.fullmatch(name):
            return {}, "invalid_attribute_name"
        if name in _RESERVED_NAMES:
            return {}, "reserved_attribute"
        if name in attributes:
            return {}, "duplicate_attribute"
        attributes[name] = path
    return attributes, None


def _valid_path(text: str, *, relative: bool) -> bool:
    """Tell whether a path parses."""
    try:
        parse_path(text, relative=relative)
    except _PathError:
        return False
    return True


def _settings_schema(kind: str) -> dict[vol.Marker, Any]:
    """Return a kind's settings fields."""
    if kind == "sensor":
        return {
            vol.Optional("value_type"): _choice(tuple(ValueType), "value_type"),
            vol.Optional("device_class"): _plain_choice(
                tuple(item for item in SensorDeviceClass if item is not SensorDeviceClass.ENUM)
            ),
            vol.Optional("unit_of_measurement"): _text_field(),
            vol.Optional("state_class"): _plain_choice(tuple(SensorStateClass)),
            vol.Optional("suggested_display_precision"): _number_field(0, 9),
            vol.Optional("naive_local"): BooleanSelector(),
        }
    if kind == "binary_sensor":
        return {
            vol.Optional("device_class"): _plain_choice(tuple(BinarySensorDeviceClass)),
            vol.Optional("on_values"): _text_field(),
            vol.Optional("off_values"): _text_field(),
        }
    if kind == "calendar":
        return {vol.Optional("naive_local"): BooleanSelector()}
    return {}


def _validate_settings(settings: Mapping[str, Any]) -> str | None:
    """Return an error key for sensor settings that do not fit together."""
    device_class = settings.get("device_class")
    sensor_class = SensorDeviceClass(device_class) if device_class else None
    value_type = settings.get("value_type")
    if sensor_class in _TIME_CLASSES:
        if value_type != ValueType.TIMESTAMP:
            return "value_type_mismatch"
    elif (
        sensor_class is not None
        or settings.get("state_class")
        or settings.get("unit_of_measurement")
    ) and value_type != ValueType.NUMBER:
        return "value_type_mismatch"
    if sensor_class is None:
        return None
    units = DEVICE_CLASS_UNITS.get(sensor_class)
    if units is not None and settings.get("unit_of_measurement") not in units:
        return "unit_mismatch"
    state_class = settings.get("state_class")
    allowed = DEVICE_CLASS_STATE_CLASSES.get(sensor_class)
    if state_class and allowed is not None and SensorStateClass(state_class) not in allowed:
        return "state_class_mismatch"
    return None


def _clean_settings(settings: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Drop empty settings, and store whole numbers."""
    clean: dict[str, JsonValue] = {}
    for key, value in settings.items():
        if value in ("", None):
            continue
        clean[key] = int(value) if isinstance(value, float) else value
    return clean


def _failure_error(check: Check) -> tuple[str | None, bool]:
    """Return an error key, and whether saving anyway may be offered."""
    if check.empty:
        return "list_empty", True
    if check.failure is None:
        return None, False
    if check.failure is Failure.TYPE_MISMATCH:
        return "wrong_data", False
    return "nothing_read", True


class _MappingFlow(ConfigSubentryFlow):
    """What sensor, tracker and collection flows share."""

    def __init__(self) -> None:
        """Start empty."""
        self._title = ""
        self._roles: dict[str, str] = {}
        self._settings: dict[str, JsonValue] = {}
        self._attributes: dict[str, str] = {}
        self._kind = ""
        self._list_path: str | None = None
        self._key_path: str | None = None
        self._listed = Flattened((), (), False)
        self._suggestions: list[tuple[CollectionCandidate, CollectionSuggestion]] = []
        self._offer_save_anyway = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Start with the one screen of each flow."""
        raise NotImplementedError

    def _coordinator(self) -> EndpointCoordinator | None:
        """Return the endpoint's coordinator, if it is running."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        coordinator: EndpointCoordinator = entry.runtime_data
        return coordinator

    async def _async_list_data(self) -> None:
        """List the stored data, away from the event loop."""
        coordinator = self._coordinator()
        if coordinator is not None and coordinator.stored.has_body:
            self._listed, self._suggestions = await self.hass.async_add_executor_job(
                _list_data, coordinator.stored.body
            )

    async def _async_refresh(self, errors: dict[str, str]) -> dict[str, str]:
        """Fetch fresh data, and return placeholders for any refusal."""
        coordinator = self._coordinator()
        if coordinator is None:
            errors["base"] = "not_loaded"
            return {}
        decision = coordinator.policy.check_manual()
        if not decision.allowed:
            errors["base"] = "refresh_refused"
            moment = decision.possible_at
            return {"time": dt_util.as_local(moment).strftime("%H:%M") if moment else "?"}
        try:
            await coordinator.async_fetch_sample()
        except HomeAssistantError:
            errors["base"] = "refresh_refused"
            return {"time": "?"}
        return {}

    def _load_subentry(self) -> None:
        """Fill the flow from the subentry being changed."""
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        self._title = subentry.title
        self._kind = str(data.get("kind", self._kind))
        self._roles = dict(data.get("roles", {}))
        self._settings = dict(data.get("settings", {}))
        self._attributes = dict(data.get(ATTRIBUTES, {}))
        self._list_path = data.get(LIST_PATH)
        self._key_path = data.get(KEY_PATH)

    def _mapping_type(self) -> str:
        """Return the subentry type."""
        return COLLECTION if self._list_path is not None else self._kind

    def _stored_data(self) -> dict[str, JsonValue]:
        """Build stored data."""
        return mapping_data(
            self._kind,
            self._roles,
            self._settings,
            self._attributes,
            list_path=self._list_path,
            key_path=self._key_path,
        )

    def _attribute_rows(self) -> list[dict[str, str]]:
        """Show stored attributes in the editor."""
        return [{"name": name, "path": path} for name, path in self._attributes.items()]

    def _save(self) -> SubentryFlowResult:
        """Create or update the subentry. The update listener then reloads."""
        data = self._stored_data()
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._title,
                data=data,
            )
        return self.async_create_entry(title=self._title, data=data)

    def _save_anyway_field(self, fields: dict[vol.Marker, Any]) -> None:
        """Offer saving a mapping that reads nothing now, when that can be right."""
        if self._offer_save_anyway:
            fields[vol.Optional(SAVE_ANYWAY, default=False)] = BooleanSelector()


class SingleMappingFlow(_MappingFlow):
    """Maps fields to entities. Subclasses name their kind."""

    kind_name: ClassVar[str] = ""

    @property
    def _definition(self) -> KindDefinition:
        """Return this flow's kind."""
        return KINDS[self.kind_name]

    def _fields(self) -> list[FlatField]:
        """Return fields this kind can use, by a readable label."""
        return [
            item
            for item in self._listed.fields
            if item.type not in (ScalarType.NULL, ScalarType.GEOMETRY)
            or (self.kind_name == "device_tracker" and item.type is ScalarType.GEOMETRY)
        ]

    def _schema(self, user_input: Mapping[str, Any] | None) -> vol.Schema:
        """Build the one screen."""
        fields = self._fields()
        main: dict[vol.Marker, Any] = {}
        if self.kind_name == "device_tracker":
            main[vol.Required(TITLE)] = _text_field()
            for role in self._definition.roles:
                main[vol.Optional(role.name)] = _labelled_field(fields)
        elif self.source == "reconfigure":
            main[vol.Required(TITLE)] = _text_field()
            main[vol.Optional("value")] = _labelled_field(fields)
        else:
            main[vol.Optional(SELECTED)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=item.path, label=_label(item)) for item in fields
                    ],
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        self._save_anyway_field(main)
        advanced: dict[vol.Marker, Any] = dict(_settings_schema(self.kind_name))
        if self.kind_name != "device_tracker" and self.source != "reconfigure":
            advanced[vol.Optional(TITLE)] = _text_field()
        advanced[vol.Optional(ATTRIBUTES)] = _attribute_field(fields)
        advanced[vol.Optional(REFRESH)] = BooleanSelector()
        main[vol.Optional(ADVANCED)] = section(vol.Schema(advanced), {"collapsed": True})
        previous = user_input or {}
        suggested: dict[str, Any] = {
            TITLE: self._title,
            **self._roles,
            **previous,
            ADVANCED: {
                **self._settings,
                ATTRIBUTES: self._attribute_rows(),
                **previous.get(ADVANCED, {}),
            },
        }
        return self.add_suggested_values_to_schema(vol.Schema(main), suggested)

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Show one screen, and create what it asks for."""
        self._kind = self.kind_name
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"time": "", "summary": ""}
        advanced = user_input.get(ADVANCED, {}) if user_input is not None else {}
        if advanced.get(REFRESH):
            placeholders.update(await self._async_refresh(errors))
        await self._async_list_data()
        if user_input is not None and not advanced.get(REFRESH):
            result = await self._async_submit(user_input, errors, placeholders)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_submit(
        self, user_input: dict[str, Any], errors: dict[str, str], placeholders: dict[str, str]
    ) -> SubentryFlowResult | None:
        """Check the answers, and save them if they read something."""
        advanced = user_input.get(ADVANCED, {})
        rows = advanced.get(ATTRIBUTES, self._attribute_rows())
        attributes, error = _read_attribute_rows(rows, relative=False)
        if error is not None:
            errors["base"] = error
            return None
        self._attributes = attributes
        paths = self._chosen_paths(user_input)
        if self.kind_name == "device_tracker":
            return await self._async_save_tracker(user_input, errors, placeholders)
        if not paths:
            errors["base"] = "choose_field"
            return None
        if not all(_valid_path(path, relative=False) for path in paths):
            errors["base"] = "invalid_path"
            return None
        if len(paths) > 1:
            return await self._async_save_several(paths, user_input, errors, placeholders)
        return await self._async_save_one(paths[0], user_input, errors, placeholders)

    def _chosen_paths(self, user_input: Mapping[str, Any]) -> list[str]:
        """Return the fields chosen."""
        if self.source == "reconfigure":
            value = user_input.get("value") or self._roles.get("value")
            return [str(value).strip()] if value else []
        return [str(path).strip() for path in user_input.get(SELECTED, [])]

    def _suggestion(self, path: str) -> FieldSuggestion:
        """Propose settings for a field of this kind."""
        by_path = {item.path: item for item in self._listed.fields}
        return suggest_field(by_path.get(path, FlatField(path, ScalarType.TEXT, "")))

    def _field_mapping(self, path: str) -> tuple[str, dict[str, JsonValue]]:
        """Build a title and stored data for a field, in this flow's kind."""
        suggestion = self._suggestion(path)
        data = field_data(suggestion, path)
        if suggestion.kind.value != self.kind_name:
            value_type = ValueType.TEXT.value
            settings: dict[str, JsonValue] = (
                {"value_type": value_type} if self.kind_name == "sensor" else {}
            )
            data = mapping_data(self.kind_name, {"value": path}, settings, {})
        return suggestion.name, data

    async def _async_save_several(
        self,
        paths: Sequence[str],
        user_input: Mapping[str, Any],
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> SubentryFlowResult | None:
        """Add several fields at once. A flow ends in one subentry, so others come first."""
        reads: list[tuple[str, dict[str, JsonValue]]] = []
        for path in paths:
            title, data = self._field_mapping(path)
            check = await self._async_check_single(title, data)
            error, offer = _failure_error(check)
            if error is not None and not (offer and user_input.get(SAVE_ANYWAY)):
                errors["base"] = error
                self._offer_save_anyway = offer
                placeholders["summary"] = f"{label_for(path)}: {check.summary}"
                return None
            reads.append((title, data))
        entry = self._get_entry()
        for title, data in reads[:-1]:
            self.hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=self.kind_name,
                    title=title,
                    unique_id=None,
                ),
            )
        title, data = reads[-1]
        return self.async_create_entry(title=title, data=data)

    async def _async_check_single(self, title: str, data: dict[str, JsonValue]) -> Check:
        """Read a mapping from stored data."""
        coordinator = self._coordinator()
        mapping = parse_mapping("preview", title, self.kind_name, data)
        if coordinator is None or mapping is None:
            return Check("", Failure.TYPE_MISMATCH)
        texts = await async_load_texts(self.hass)
        return await self.hass.async_add_executor_job(
            check_single,
            coordinator.stored.body,
            mapping,
            dt_util.utcnow(),
            dt_util.get_default_time_zone(),
            texts,
        )

    async def _async_save_one(
        self,
        path: str,
        user_input: Mapping[str, Any],
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> SubentryFlowResult | None:
        """Save one field, with what the advanced section changed."""
        advanced = user_input.get(ADVANCED, {})
        title, data = self._field_mapping(path)
        typed = _clean_settings(
            {
                key: value
                for key, value in advanced.items()
                if key not in (ATTRIBUTES, REFRESH, TITLE)
            }
        )
        suggested = data["settings"]
        base: dict[str, JsonValue] = dict(suggested) if isinstance(suggested, dict) else {}
        kept = self._settings if self.source == "reconfigure" else base
        self._settings = {**kept, **typed}
        if self.kind_name == "sensor":
            self._settings.setdefault("value_type", ValueType.TEXT.value)
            error = _validate_settings(self._settings)
            if error is not None:
                errors["base"] = error
                return None
        self._roles = {"value": path}
        self._title = str(user_input.get(TITLE) or advanced.get(TITLE) or title)
        return await self._async_check_and_save(user_input, errors, placeholders)

    async def _async_save_tracker(
        self,
        user_input: Mapping[str, Any],
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> SubentryFlowResult | None:
        """Save a position."""
        roles = {
            role.name: str(user_input[role.name]).strip()
            for role in self._definition.roles
            if user_input.get(role.name)
        }
        if not all(_valid_path(text, relative=False) for text in roles.values()):
            errors["base"] = "invalid_path"
            return None
        validate = self._definition.validate
        error = validate(roles) if validate is not None else None
        if error is not None:
            errors["base"] = error
            return None
        self._roles = roles
        self._title = str(user_input[TITLE])
        return await self._async_check_and_save(user_input, errors, placeholders)

    async def _async_check_and_save(
        self,
        user_input: Mapping[str, Any],
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> SubentryFlowResult | None:
        """Refuse a mapping that reads nothing, unless told to keep it."""
        check = await self._async_check_single(self._title, self._stored_data())
        error, offer = _failure_error(check)
        if error is None or (offer and user_input.get(SAVE_ANYWAY)):
            return self._save()
        errors["base"] = error
        self._offer_save_anyway = offer
        placeholders["summary"] = check.summary
        return None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the mapping."""
        self._load_subentry()
        return await self.async_step_user()


def _labelled_field(fields: Sequence[FlatField]) -> SelectSelector:
    """Return a searchable list of fields by label, which also takes a typed path."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=item.path, label=_label(item)) for item in fields],
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _single_flow(kind: str) -> type[SingleMappingFlow]:
    """Build the flow of a kind."""

    class Flow(SingleMappingFlow):
        kind_name: ClassVar[str] = kind

    return Flow


class CollectionFlow(_MappingFlow):
    """Maps a list on one screen, starting from a proposal."""

    def __init__(self) -> None:
        """Start with no proposal."""
        super().__init__()
        self._proposed = False

    def _candidate(self) -> CollectionCandidate | None:
        """Return the chosen list."""
        for candidate, _ in self._suggestions:
            if candidate.path == self._list_path:
                return candidate
        return None

    def _suggestion(self) -> CollectionSuggestion | None:
        """Return the proposal for the chosen list."""
        for candidate, suggestion in self._suggestions:
            if candidate.path == self._list_path:
                return suggestion
        return None

    def _fallback_title(self) -> str:
        """Name a list at the root after the endpoint."""
        return (URL(str(self._get_entry().data[CONF_URL])).host or "List").partition(".")[0]

    def _apply_proposal(self, kind: str | None = None) -> None:
        """Fill the flow from the proposal for the chosen list."""
        suggestion = self._suggestion()
        candidate = self._candidate()
        if suggestion is None or candidate is None:
            self._kind = kind or self._kind or "sensor"
            self._key_path = self._key_path or ""
            return
        self._kind = kind or suggestion.kind.value
        self._key_path = self._key_path or suggestion.key
        self._title = self._title or collection_title(candidate.path, self._fallback_title())
        self._roles = roles_for(self._kind, suggestion)
        self._settings = collection_settings(self._kind, candidate, suggestion, self._title)
        self._proposed = True

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the list, unless there is only one."""
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"time": ""}
        if user_input is not None and user_input.get(REFRESH):
            placeholders.update(await self._async_refresh(errors))
        await self._async_list_data()
        if self.source == "reconfigure":
            return await self.async_step_proposal()
        lists = [candidate for candidate, _ in self._suggestions]
        if user_input is None and len(lists) == 1:
            self._list_path = lists[0].path
            return await self.async_step_proposal()
        if user_input is not None and not user_input.get(REFRESH):
            path = str(user_input.get(LIST_PATH, "")).strip()
            if path and _valid_path(path, relative=False):
                self._list_path = path
                self._key_path = None
                self._title = ""
                return await self.async_step_proposal()
            errors["base"] = "invalid_path"
        options = [
            SelectOptionDict(value=item.path, label=f"{label_for(item.path)} ({item.length})")
            for item in lists
        ]
        schema = vol.Schema(
            {
                vol.Optional(LIST_PATH): SelectSelector(
                    SelectSelectorConfig(
                        options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(REFRESH): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    def _proposal_schema(self, texts_fields: Sequence[FlatField]) -> vol.Schema:
        """Build the one screen."""
        definition = KINDS[self._kind]
        kinds = [
            name
            for name, item in KINDS.items()
            if item.available() and item.shapes & {Shape.ITEM, Shape.WHOLE_LIST}
        ]
        main: dict[vol.Marker, Any] = {
            vol.Required(TITLE): _text_field(),
            vol.Required(KIND): _choice(kinds, "kind"),
        }
        self._save_anyway_field(main)
        advanced: dict[vol.Marker, Any] = {vol.Optional(KEY_PATH): _labelled_field(texts_fields)}
        if Shape.ITEM in definition.shapes:
            advanced[vol.Optional(ROLE_NAME)] = _labelled_field(texts_fields)
        for role in definition.roles:
            advanced[vol.Optional(role.name)] = _labelled_field(texts_fields)
        advanced.update(_settings_schema(self._kind))
        advanced[vol.Optional(ITEM_LIMIT)] = _number_field(1, MAX_ITEM_LIMIT)
        advanced[vol.Optional(REMOVAL_AFTER)] = _number_field(1, 10)
        advanced[vol.Optional(EVENTS)] = BooleanSelector()
        if definition.has_attributes:
            advanced[vol.Optional(ATTRIBUTES)] = _attribute_field(texts_fields)
        advanced[vol.Optional(REFRESH)] = BooleanSelector()
        main[vol.Optional(ADVANCED)] = section(vol.Schema(advanced), {"collapsed": True})
        suggested: dict[str, Any] = {
            TITLE: self._title,
            KIND: self._kind,
            ADVANCED: self._advanced_defaults(),
        }
        return self.add_suggested_values_to_schema(vol.Schema(main), suggested)

    def _advanced_defaults(self) -> dict[str, Any]:
        """Return the proposal, as a section sends only fields that were changed."""
        return {
            KEY_PATH: self._key_path,
            **self._roles,
            **self._settings,
            ATTRIBUTES: self._attribute_rows(),
        }

    def _read_advanced(self, user_input: Mapping[str, Any]) -> str | None:
        """Take the form's answers, and return an error key."""
        advanced = user_input.get(ADVANCED, self._advanced_defaults())
        definition = KINDS[self._kind]
        names = [role.name for role in definition.roles]
        if Shape.ITEM in definition.shapes:
            names.append(ROLE_NAME)
        roles = {name: str(advanced[name]).strip() for name in names if advanced.get(name)}
        for role in definition.roles:
            if role.required and role.name not in roles:
                return "choose_field"
        if definition.validate is not None and (error := definition.validate(roles)):
            return error
        key = str(advanced.get(KEY_PATH) or "").strip()
        if not key:
            return "choose_key"
        if not all(_valid_path(text, relative=True) for text in (*roles.values(), key)):
            return "invalid_path"
        attributes, error = _read_attribute_rows(advanced.get(ATTRIBUTES, []), relative=True)
        if error is not None:
            return error
        settings_keys = {str(marker) for marker in _settings_schema(self._kind)}
        settings = _clean_settings(
            {
                name: advanced[name]
                for name in (*settings_keys, ITEM_LIMIT, REMOVAL_AFTER, EVENTS)
                if name in advanced
            }
        )
        if self._kind == "sensor":
            error = _validate_settings(settings)
            if error is not None:
                return error
        source = self._settings.get("source")
        self._settings = settings
        self._settings["source"] = source if source else slugify(str(user_input[TITLE]))
        self._roles = roles
        self._key_path = key
        self._attributes = attributes
        self._title = str(user_input[TITLE])
        return None

    async def async_step_proposal(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the proposal, ready to confirm."""
        if self.source == "reconfigure" and not self._kind:
            self._load_subentry()
            self._proposed = True
        if not self._proposed:
            self._apply_proposal()
        errors: dict[str, str] = {}
        placeholders = {"summary": "", "time": ""}
        if user_input is not None:
            kind = str(user_input.get(KIND, self._kind))
            advanced = user_input.get(ADVANCED, {})
            if advanced.get(REFRESH):
                placeholders.update(await self._async_refresh(errors))
                await self._async_list_data()
            elif kind != self._kind:
                self._title = str(user_input.get(TITLE, self._title))
                self._apply_proposal(kind)
            else:
                result = await self._async_submit(user_input, errors)
                if result is not None:
                    return result
        texts = await async_load_texts(self.hass)
        placeholders["summary"] = await self._async_summary(texts)
        candidate = self._candidate()
        fields = candidate.item_fields if candidate is not None else ()
        return self.async_show_form(
            step_id="proposal",
            data_schema=self._proposal_schema(fields),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_check(self, texts: Texts) -> Check | None:
        """Read the mapping as it stands from stored data."""
        coordinator = self._coordinator()
        mapping = parse_mapping("preview", self._title, COLLECTION, self._stored_data())
        if coordinator is None or mapping is None:
            return None
        return await self.hass.async_add_executor_job(
            check_collection,
            coordinator.stored.body,
            mapping,
            dt_util.utcnow(),
            dt_util.get_default_time_zone(),
            texts,
        )

    async def _async_summary(self, texts: Texts) -> str:
        """Say what the proposal will create."""
        check = await self._async_check(texts)
        summary = check.summary if check is not None else ""
        mapping = parse_mapping("preview", self._title, COLLECTION, self._stored_data())
        if self._kind == "geo_location" and mapping is not None:
            return f"{summary}\n\n{texts('map_hint', source=source_for(mapping))}"
        return summary

    async def _async_submit(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> SubentryFlowResult | None:
        """Save, unless the mapping reads nothing."""
        error = self._read_advanced(user_input)
        if error is not None:
            errors["base"] = error
            return None
        texts = await async_load_texts(self.hass)
        check = await self._async_check(texts)
        if check is None:
            errors["base"] = "invalid_path"
            return None
        problem, offer = _failure_error(check)
        if problem is None or (offer and user_input.get(SAVE_ANYWAY)):
            return self._save()
        errors["base"] = problem
        self._offer_save_anyway = offer
        return None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the collection."""
        return await self.async_step_user()
