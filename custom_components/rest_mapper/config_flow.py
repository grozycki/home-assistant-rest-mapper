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
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
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
from .evaluation import read_value
from .kinds import KINDS, KindDefinition, MappingConfig, Shape
from .lib import (
    DEFAULT_ITEM_LIMIT,
    DEFAULT_MAX_BYTES,
    DEFAULT_REMOVAL_AFTER,
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
    ConnectFailedError,
    Fetched,
    FetchError,
    FlatField,
    Flattened,
    FormatError,
    JsonValue,
    Position,
    RateLimitedError,
    RedirectError,
    RequestPolicy,
    ServerFailedError,
    SizeExceededError,
    UpdateMode,
    ValueType,
    decode_json,
    fetch,
    flatten,
    parse_path,
    read_items,
)
from .lib import PathError as _PathError
from .mappings import mapping_data, parse_mapping, source_for
from .store import StoredEndpoint, async_load, endpoint_store
from .suggest import guess_sensor, last_key, settings_for_field, value_type_of
from .values import ValueRead

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .coordinator import EndpointCoordinator

ATTRIBUTES: Final = "attributes"
TITLE: Final = "title"
REFRESH: Final = "refresh_sample"
BACK: Final = "back"
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
_PREVIEW_ITEMS: Final = 3
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


def _path_field(fields: Sequence[FlatField]) -> SelectSelector:
    """Return a searchable list of paths that also takes typed text."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=item.path, label=f"{item.path} = {item.example}")
                for item in fields
            ],
            custom_value=True,
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


def _cost_placeholders(options: Mapping[str, Any]) -> dict[str, str]:
    """Describe what a polling choice uses."""
    if options.get(CONF_MODE) == UpdateMode.ON_DEMAND:
        return {"per_day": "0", "per_month": "0", "budget_note": ""}
    per_day = _DAY_SECONDS / float(options[CONF_INTERVAL])
    per_month = per_day * _MONTH_DAYS
    config = build_policy_config(options)
    spacing = RequestPolicy(config, dt_util.utcnow, random.random).spacing()
    slower = spacing > config.interval
    return {
        "per_day": f"{per_day:.0f}",
        "per_month": f"{per_month:.0f}",
        "budget_note": f"{spacing.total_seconds() / 60:.0f}" if slower else "",
    }


class RestMapperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adds an endpoint."""

    VERSION = 1

    def __init__(self) -> None:
        """Start empty."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._result: _TestResult | None = None

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

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the URL and the type of authentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = dict(user_input)
            error = _validate_url(user_input[CONF_URL])
            if error is not None:
                errors[CONF_URL] = error
            elif user_input[CONF_AUTH_TYPE] == AUTH_NONE:
                return await self._async_test()
            else:
                return await self.async_step_auth()
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): _text_field(),
                vol.Required(CONF_AUTH_TYPE, default=AUTH_NONE): _choice(
                    (AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_QUERY), "auth_type"
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, self._data),
            errors=errors,
        )

    def _auth_schema(self, auth_type: str) -> vol.Schema:
        """Return credential fields."""
        fields: dict[vol.Marker, Any] = {}
        if auth_type in (AUTH_HEADER, AUTH_QUERY):
            fields[vol.Required(CONF_AUTH_NAME)] = _text_field()
        fields[vol.Required(CONF_AUTH_VALUE)] = _text_field(password=True)
        return vol.Schema(fields)

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for credentials."""
        if user_input is not None:
            self._data.update(user_input)
            if _is_public_host(URL(self._data[CONF_URL]).host) and self._is_plain_http():
                return await self.async_step_insecure()
            return await self._async_test()
        return self.async_show_form(
            step_id="auth", data_schema=self._auth_schema(self._data[CONF_AUTH_TYPE])
        )

    def _is_plain_http(self) -> bool:
        """Tell whether credentials would travel unencrypted."""
        return URL(self._data[CONF_URL]).scheme == "http"

    async def async_step_insecure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn about credentials on plain HTTP."""
        if user_input is not None:
            return await self._async_test()
        return self.async_show_form(step_id="insecure", data_schema=vol.Schema({}))

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
        await self.async_set_unique_id(_unique_id(self._data))
        self._abort_if_unique_id_configured()
        error = await self._async_run_test(self._data)
        if error is not None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_URL, default=self._data[CONF_URL]): _text_field(),
                        vol.Required(CONF_AUTH_TYPE, default=self._data[CONF_AUTH_TYPE]): _choice(
                            (AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_QUERY), "auth_type"
                        ),
                    }
                ),
                errors={"base": error},
            )
        return await self.async_step_polling()

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how often to poll."""
        if user_input is not None:
            self._options = _options_defaults(user_input)
            self._options[CONF_RETENTION] = _suggested_retention(self._options)
            return await self.async_step_cost()
        schema = self.add_suggested_values_to_schema(
            vol.Schema(_polling_schema()), _options_defaults(self._options)
        )
        return self.async_show_form(step_id="polling", data_schema=schema)

    async def async_step_cost(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show what the polling choice uses."""
        if user_input is not None:
            return await self.async_step_fields()
        return self.async_show_form(
            step_id="cost",
            data_schema=vol.Schema({}),
            description_placeholders=_cost_placeholders(self._options),
        )

    async def async_step_fields(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer the found fields as first mappings."""
        result = self._result
        assert result is not None
        if user_input is not None:
            return await self._async_finish(result, user_input.get(SELECTED, []))
        options = [
            SelectOptionDict(value=item.path, label=f"{item.path} = {item.example}")
            for item in result.found.fields
        ]
        schema = vol.Schema(
            {
                vol.Optional(SELECTED, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="fields", data_schema=schema)

    async def _async_finish(self, result: _TestResult, selected: Sequence[str]) -> ConfigFlowResult:
        """Store the sample, and create the entry with its mappings."""
        storage_id = secrets.token_hex(8)
        policy = RequestPolicy(build_policy_config(self._options), dt_util.utcnow, random.random)
        policy.record_sent()
        policy.record_fetched(result.fetched.etag, result.fetched.last_modified)
        stored = StoredEndpoint(
            body=result.body,
            has_body=True,
            policy=policy.state,
            confirmed_at=dt_util.utcnow(),
        )
        await endpoint_store(self.hass, storage_id).async_save(stored.to_data())
        fields = {item.path: item for item in result.found.fields}
        subentries = [_bulk_subentry(fields[path]) for path in selected if path in fields]
        return self.async_create_entry(
            title=URL(self._data[CONF_URL]).host or "REST Mapper",
            data={**self._data, CONF_STORAGE_ID: storage_id},
            options=self._options,
            subentries=subentries,
        )

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
            data = {**entry.data, **user_input}
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
            fields.update(self._auth_schema(entry.data[CONF_AUTH_TYPE]).schema)
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), {CONF_URL: entry.data[CONF_URL]}
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def _async_apply(self, entry: ConfigEntry, data: dict[str, Any]) -> None:
        """Replace the sample and settings. The update listener then reloads."""
        result = self._result
        assert result is not None
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


def _bulk_subentry(field: FlatField) -> ConfigSubentryData:
    """Build a mapping for a field picked during setup."""
    settings = settings_for_field(field)
    is_boolean = settings["value_type"] == ValueType.BOOLEAN
    kind = "binary_sensor" if is_boolean else "sensor"
    if is_boolean:
        settings = {}
    return ConfigSubentryData(
        data=mapping_data(kind, {"value": field.path}, settings, {}),
        subentry_type=kind,
        title=last_key(field.path).replace("_", " ").capitalize(),
        unique_id=None,
    )


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
                            "options": [item.path for item in paths],
                            "custom_value": True,
                        }
                    },
                ),
            },
        )
    )


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
        name = row.get("name", "").strip() or slugify(last_key(path, relative=relative))
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
            vol.Required("value_type"): _choice(tuple(ValueType), "value_type"),
            vol.Optional("device_class"): _plain_choice(
                tuple(item for item in SensorDeviceClass if item is not SensorDeviceClass.ENUM)
            ),
            vol.Optional("unit_of_measurement"): _text_field(),
            vol.Optional("state_class"): _plain_choice(tuple(SensorStateClass)),
            vol.Optional("suggested_display_precision"): _number_field(0, 9),
            vol.Optional("naive_local", default=False): BooleanSelector(),
        }
    if kind == "binary_sensor":
        return {
            vol.Optional("device_class"): _plain_choice(tuple(BinarySensorDeviceClass)),
            vol.Optional("on_values"): _text_field(),
            vol.Optional("off_values"): _text_field(),
        }
    if kind == "calendar":
        return {vol.Optional("naive_local", default=False): BooleanSelector()}
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


def _describe(value: object, mapping: MappingConfig) -> str:
    """Write a read value for the confirmation step."""
    if isinstance(value, Position):
        return f"{value.latitude}, {value.longitude}"
    unit = mapping.settings.get("unit_of_measurement")
    return f"{value} {unit}" if isinstance(unit, str) and unit else str(value)


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
            self._listed = await self.hass.async_add_executor_job(flatten, coordinator.stored.body)

    def _found(self) -> Flattened:
        """Return what `_async_list_data` found."""
        return self._listed

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

    @property
    def _relative(self) -> bool:
        """Tell whether role paths are relative to a list item."""
        return self._list_path is not None

    def _item_fields(self) -> Sequence[FlatField]:
        """Return the paths offered for roles and attributes."""
        found = self._found()
        if not self._relative:
            return found.fields
        for candidate in found.collections:
            if candidate.path == self._list_path:
                return candidate.item_fields
        return ()

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

    def _mapping(self) -> MappingConfig | None:
        """Build the mapping as it stands."""
        return parse_mapping("preview", self._title, self._mapping_type(), self._data())

    def _mapping_type(self) -> str:
        """Return the subentry type."""
        return COLLECTION if self._relative else self._kind

    def _data(self) -> dict[str, JsonValue]:
        """Build stored data."""
        return mapping_data(
            self._kind,
            self._roles,
            self._settings,
            self._attributes,
            list_path=self._list_path,
            key_path=self._key_path,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Start with choosing paths."""
        raise NotImplementedError

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the kind's settings."""
        fields = _settings_schema(self._kind)
        if not fields:
            return await self._async_after_settings()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_settings(user_input) if self._kind == "sensor" else None
            kept: dict[str, JsonValue] = {
                key: self._settings[key]
                for key in (ITEM_LIMIT, REMOVAL_AFTER, EVENTS, "source")
                if key in self._settings
            }
            self._settings = {**kept, **_clean_settings(user_input)}
            if error is None:
                return await self._async_after_settings()
            errors["base"] = error
        schema = self.add_suggested_values_to_schema(vol.Schema(fields), self._settings)
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)

    async def _async_after_settings(self) -> SubentryFlowResult:
        """Move on after settings."""
        return await self.async_step_attributes()

    async def async_step_attributes(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for extra attributes."""
        if not KINDS[self._kind].has_attributes:
            return await self.async_step_confirm()
        errors: dict[str, str] = {}
        if user_input is not None:
            attributes, error = _read_attribute_rows(
                user_input.get(ATTRIBUTES, []), relative=self._relative
            )
            if error is None:
                self._attributes = attributes
                return await self.async_step_confirm()
            errors["base"] = error
        rows = [{"name": name, "path": path} for name, path in self._attributes.items()]
        schema = vol.Schema(
            {vol.Optional(ATTRIBUTES, default=rows): _attribute_field(self._item_fields())}
        )
        return self.async_show_form(step_id="attributes", data_schema=schema, errors=errors)

    def _preview(self) -> str:
        """Describe what the mapping reads from stored data."""
        raise NotImplementedError

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show what the mapping reads, and save it."""
        if user_input is not None:
            if user_input.get(BACK):
                return await self.async_step_user()
            return await self._async_save()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional(BACK, default=False): BooleanSelector()}),
            description_placeholders={
                "preview": await self.hass.async_add_executor_job(self._preview)
            },
        )

    async def _async_save(self) -> SubentryFlowResult:
        """Create or update the subentry. The update listener then reloads."""
        data = self._data()
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=self._title,
                data=data,
            )
        return self.async_create_entry(title=self._title, data=data)


class SingleMappingFlow(_MappingFlow):
    """Maps one entity. Subclasses name their kind."""

    kind_name: ClassVar[str] = ""

    @property
    def _definition(self) -> KindDefinition:
        """Return this flow's kind."""
        return KINDS[self.kind_name]

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose paths."""
        self._kind = self.kind_name
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None and user_input.get(REFRESH):
            placeholders = await self._async_refresh(errors)
        await self._async_list_data()
        if user_input is not None and not user_input.get(REFRESH):
            error = self._read_roles(user_input)
            if error is None:
                return await self.async_step_settings()
            errors["base"] = error
        fields = self._found().fields
        schema_fields: dict[vol.Marker, Any] = {vol.Required(TITLE): _text_field()}
        for role in self._definition.roles:
            marker = vol.Required if role.required else vol.Optional
            schema_fields[marker(role.name)] = _path_field(fields)
        schema_fields[vol.Optional(REFRESH, default=False)] = BooleanSelector()
        suggested = {TITLE: self._title, **self._roles}
        schema = self.add_suggested_values_to_schema(vol.Schema(schema_fields), suggested)
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"time": "", **placeholders},
        )

    def _read_roles(self, user_input: Mapping[str, Any]) -> str | None:
        """Keep the chosen paths, or return an error key."""
        roles = {
            role.name: str(user_input[role.name]).strip()
            for role in self._definition.roles
            if user_input.get(role.name)
        }
        if not all(_valid_path(text, relative=False) for text in roles.values()):
            return "invalid_path"
        validate = self._definition.validate
        error = validate(roles) if validate is not None else None
        if error is not None:
            return error
        self._title = str(user_input[TITLE])
        self._roles = roles
        if self._kind == "sensor" and "value_type" not in self._settings:
            self._suggest_sensor(roles["value"])
        return None

    def _suggest_sensor(self, path: str) -> None:
        """Fill in suggested sensor settings."""
        by_path = {item.path: item for item in self._found().fields}
        guess = guess_sensor(path)
        self._settings["value_type"] = value_type_of(by_path.get(path)).value
        if guess.device_class is not None:
            self._settings["device_class"] = guess.device_class.value
        if guess.unit is not None:
            self._settings["unit_of_measurement"] = guess.unit

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the mapping."""
        self._load_subentry()
        return await self.async_step_user()

    @override
    def _preview(self) -> str:
        """Describe the read value."""
        mapping = self._mapping()
        coordinator = self._coordinator()
        if mapping is None or coordinator is None:
            return "?"
        read = read_value(
            coordinator.stored.body,
            mapping,
            ValueRead(),
            dt_util.utcnow(),
            dt_util.get_default_time_zone(),
        )
        if read.failure is not None:
            return f"Cannot read: {read.failure.value}"
        return _describe(read.value, mapping)


def _single_flow(kind: str) -> type[SingleMappingFlow]:
    """Build the flow of a kind."""

    class Flow(SingleMappingFlow):
        kind_name: ClassVar[str] = kind

    return Flow


class CollectionFlow(_MappingFlow):
    """Maps a list."""

    def _candidates(self) -> tuple[CollectionCandidate, ...]:
        """Return lists in the stored data."""
        return self._found().collections

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the list."""
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None and user_input.get(REFRESH):
            placeholders = await self._async_refresh(errors)
        await self._async_list_data()
        if user_input is not None and not user_input.get(REFRESH):
            if _valid_path(str(user_input[LIST_PATH]), relative=False):
                self._list_path = str(user_input[LIST_PATH])
                return await self.async_step_key()
            errors["base"] = "invalid_path"
        options = [
            SelectOptionDict(value=item.path, label=f"{item.path} ({item.length})")
            for item in self._candidates()
        ]
        schema = vol.Schema(
            {
                vol.Required(LIST_PATH): SelectSelector(
                    SelectSelectorConfig(
                        options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(REFRESH, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, {LIST_PATH: self._list_path}),
            errors=errors,
            description_placeholders={"time": "", **placeholders},
        )

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the key, with likely ones first."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _valid_path(str(user_input[KEY_PATH]), relative=True):
                self._key_path = str(user_input[KEY_PATH])
                return await self.async_step_kind()
            errors["base"] = "invalid_path"
        likely: tuple[str, ...] = ()
        for candidate in self._candidates():
            if candidate.path == self._list_path:
                likely = candidate.likely_keys
        others = [item.path for item in self._item_fields() if item.path not in likely]
        schema = vol.Schema(
            {vol.Required(KEY_PATH): _plain_choice([*likely, *others], custom=True)}
        )
        return self.async_show_form(
            step_id="key",
            data_schema=self.add_suggested_values_to_schema(schema, {KEY_PATH: self._key_path}),
            errors=errors,
        )

    async def async_step_kind(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose what items become."""
        if user_input is not None:
            self._kind = str(user_input[KIND])
            return await self.async_step_item()
        kinds = [
            name
            for name, definition in KINDS.items()
            if definition.available() and definition.shapes & {Shape.ITEM, Shape.WHOLE_LIST}
        ]
        schema = vol.Schema({vol.Required(KIND): _choice(kinds, "kind")})
        return self.async_show_form(
            step_id="kind",
            data_schema=self.add_suggested_values_to_schema(schema, {KIND: self._kind}),
        )

    async def async_step_item(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the paths inside an item."""
        definition = KINDS[self._kind]
        has_name = Shape.ITEM in definition.shapes
        errors: dict[str, str] = {}
        if user_input is not None:
            names = [role.name for role in definition.roles]
            if has_name:
                names.append(ROLE_NAME)
            roles = {name: str(user_input[name]).strip() for name in names if user_input.get(name)}
            error = definition.validate(roles) if definition.validate is not None else None
            if not all(_valid_path(text, relative=True) for text in roles.values()):
                error = "invalid_path"
            if error is None:
                self._title = str(user_input[TITLE])
                self._roles = roles
                if self._kind == "sensor" and "value_type" not in self._settings:
                    self._settings["value_type"] = ValueType.NUMBER.value
                return await self.async_step_settings()
            errors["base"] = error
        fields = self._item_fields()
        schema_fields: dict[vol.Marker, Any] = {vol.Required(TITLE): _text_field()}
        if has_name:
            schema_fields[vol.Optional(ROLE_NAME)] = _path_field(fields)
        for role in definition.roles:
            marker = vol.Required if role.required else vol.Optional
            schema_fields[marker(role.name)] = _path_field(fields)
        schema = self.add_suggested_values_to_schema(
            vol.Schema(schema_fields), {TITLE: self._title, **self._roles}
        )
        return self.async_show_form(step_id="item", data_schema=schema, errors=errors)

    @override
    async def _async_after_settings(self) -> SubentryFlowResult:
        """Ask for limits next."""
        return await self.async_step_limits()

    async def async_step_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for limits and events."""
        if user_input is not None:
            self._settings.update(_clean_settings(user_input))
            self._settings.setdefault("source", slugify(self._title))
            return await self.async_step_attributes()
        schema = vol.Schema(
            {
                vol.Required(ITEM_LIMIT): _number_field(1, MAX_ITEM_LIMIT),
                vol.Required(REMOVAL_AFTER): _number_field(1, 10),
                vol.Required(EVENTS): BooleanSelector(),
            }
        )
        defaults = {
            ITEM_LIMIT: DEFAULT_ITEM_LIMIT,
            REMOVAL_AFTER: DEFAULT_REMOVAL_AFTER,
            EVENTS: True,
            **self._settings,
        }
        return self.async_show_form(
            step_id="limits", data_schema=self.add_suggested_values_to_schema(schema, defaults)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the collection."""
        self._load_subentry()
        return await self.async_step_user()

    @override
    def _preview(self) -> str:
        """Describe the items found."""
        mapping = self._mapping()
        coordinator = self._coordinator()
        if (
            mapping is None
            or coordinator is None
            or mapping.list_path is None
            or mapping.key_path is None
        ):
            return "?"
        body = coordinator.stored.body
        items = read_items(body, mapping.list_path, mapping.key_path)
        if items is None:
            return f"No list at {mapping.list_path}"
        now = dt_util.utcnow()
        zone = dt_util.get_default_time_zone()
        reads = {
            key: read_value(item, mapping, ValueRead(), now, zone)
            for key, item in items.items.items()
        }
        unreadable = items.skipped + sum(1 for read in reads.values() if read.failure)
        lines = [f"Items: {len(items.items)}, skipped: {unreadable}"]
        for key, read in list(reads.items())[:_PREVIEW_ITEMS]:
            value = read.failure.value if read.failure else _describe(read.value, mapping)
            lines.append(f"{key}: {value}")
        return "\n\n".join(lines)

    @override
    async def _async_save(self) -> SubentryFlowResult:
        """Show the map card setting for positions first."""
        if self._kind == "geo_location":
            return await self.async_step_map_hint()
        return await super()._async_save()

    async def async_step_map_hint(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Say how to show positions on a map."""
        if user_input is not None:
            return await super()._async_save()
        mapping = self._mapping()
        return self.async_show_form(
            step_id="map_hint",
            data_schema=vol.Schema({}),
            description_placeholders={
                "source": source_for(mapping) if mapping is not None else DOMAIN
            },
        )
