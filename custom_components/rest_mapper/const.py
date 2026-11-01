"""Constants for REST Mapper."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "rest_mapper"
STORAGE_VERSION: Final = 1
MAPPING_VERSION: Final = 1

CONF_URL: Final = "url"
CONF_AUTH_TYPE: Final = "auth_type"
CONF_AUTH_NAME: Final = "auth_name"
CONF_AUTH_VALUE: Final = "auth_value"
CONF_STORAGE_ID: Final = "storage_id"

CONF_MODE: Final = "mode"
CONF_INTERVAL: Final = "interval_seconds"
CONF_BUDGET_LIMIT: Final = "budget_limit"
CONF_BUDGET_PERIOD: Final = "budget_period"
CONF_RESERVE: Final = "reserve_percent"
CONF_RETENTION: Final = "retention_seconds"
CONF_MAX_KIB: Final = "max_response_kib"
CONF_TIMEOUT: Final = "timeout_seconds"

AUTH_NONE: Final = "none"
AUTH_BEARER: Final = "bearer"
AUTH_HEADER: Final = "header"
AUTH_QUERY: Final = "query"

SAVE_DELAY_SECONDS: Final = 300
STARTUP_DELAY: Final = timedelta(seconds=60)
STARTUP_VARIATION_SECONDS: Final = 30
REFRESH_COOLDOWN_SECONDS: Final = 10
CONFIRMATION_WRITE_INTERVAL: Final = timedelta(minutes=15)
REPAIR_AFTER_FAILURES: Final = 3
MAX_EVENTS_PER_RESPONSE: Final = 10
MAX_ATTRIBUTES: Final = 20
EVENT_NEW_ITEM: Final = "new_item"
ATTR_LAST_CONFIRMED: Final = "last_confirmed"
ATTR_KEY: Final = "key"
ATTR_LEFT_OUT: Final = "left_out"
COLLECTION: Final = "collection"
ROLE_NAME: Final = "name"
ROLE_ICON: Final = "icon"
CONF_ICONS: Final = "icons"
MAX_ICON_TEXT: Final = 8
ANY_VALUE: Final = "*"
ICON_PREFIX: Final = "mdi:"
MAX_KEY_LENGTH: Final = 64
MAX_NAME_LENGTH: Final = 24
SHORT_KEY_LENGTH: Final = 6
MIN_RETENTION: Final = timedelta(hours=1)
RETENTION_INTERVALS: Final = 4
