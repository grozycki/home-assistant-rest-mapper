"""Read an entry's settings into library types."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from .const import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_QUERY,
    CONF_AUTH_NAME,
    CONF_AUTH_TYPE,
    CONF_AUTH_VALUE,
    CONF_BUDGET_LIMIT,
    CONF_BUDGET_PERIOD,
    CONF_INTERVAL,
    CONF_MAX_KIB,
    CONF_MODE,
    CONF_RESERVE,
    CONF_TIMEOUT,
    CONF_URL,
)
from .lib import (
    DEFAULT_INTERVAL,
    DEFAULT_MAX_BYTES,
    DEFAULT_RESERVE_PERCENT,
    DEFAULT_TIMEOUT,
    Auth,
    BearerAuth,
    Budget,
    BudgetPeriod,
    FetchSpec,
    HeaderAuth,
    NoAuth,
    PolicyConfig,
    QueryAuth,
    UpdateMode,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def build_auth(data: Mapping[str, object]) -> Auth:
    """Read authentication settings."""
    kind = data.get(CONF_AUTH_TYPE)
    name = str(data.get(CONF_AUTH_NAME, ""))
    value = str(data.get(CONF_AUTH_VALUE, ""))
    if kind == AUTH_BEARER:
        return BearerAuth(value)
    if kind == AUTH_HEADER:
        return HeaderAuth(name, value)
    if kind == AUTH_QUERY:
        return QueryAuth(name, value)
    return NoAuth()


def build_spec(data: Mapping[str, object], options: Mapping[str, object]) -> FetchSpec:
    """Read what to request."""
    kib = options.get(CONF_MAX_KIB)
    timeout = options.get(CONF_TIMEOUT)
    return FetchSpec(
        str(data[CONF_URL]),
        build_auth(data),
        int(kib) * 1024 if isinstance(kib, int | float) else DEFAULT_MAX_BYTES,
        float(timeout) if isinstance(timeout, int | float) else DEFAULT_TIMEOUT,
    )


def build_policy_config(options: Mapping[str, object]) -> PolicyConfig:
    """Read polling settings."""
    interval = options.get(CONF_INTERVAL)
    limit = options.get(CONF_BUDGET_LIMIT)
    reserve = options.get(CONF_RESERVE)
    period = options.get(CONF_BUDGET_PERIOD)
    budget = None
    if isinstance(limit, int | float) and limit > 0:
        budget = Budget(
            int(limit),
            BudgetPeriod(period) if isinstance(period, str) else BudgetPeriod.MONTH,
            int(reserve) if isinstance(reserve, int | float) else DEFAULT_RESERVE_PERCENT,
        )
    return PolicyConfig(
        UpdateMode.ON_DEMAND
        if options.get(CONF_MODE) == UpdateMode.ON_DEMAND
        else UpdateMode.AUTOMATIC,
        timedelta(seconds=interval) if isinstance(interval, int | float) else DEFAULT_INTERVAL,
        budget,
    )
