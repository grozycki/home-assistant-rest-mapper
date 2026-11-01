"""Decide when a request may happen. This module does no I/O and no sleeping."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .paths import JsonValue

DEFAULT_INTERVAL: Final = timedelta(minutes=15)
MIN_INTERVAL: Final = timedelta(seconds=10)
DEFAULT_RESERVE_PERCENT: Final = 10
BACKOFF_CEILING: Final = timedelta(hours=6)
RETRY_AFTER_CEILING: Final = timedelta(hours=24)
BACKOFF_VARIATION: Final = 0.1
_MONTHS_IN_YEAR: Final = 12


class UpdateMode(StrEnum):
    """Whether requests happen by themselves."""

    AUTOMATIC = "automatic"
    ON_DEMAND = "on_demand"


class BudgetPeriod(StrEnum):
    """A calendar period in UTC."""

    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


class Refusal(StrEnum):
    """Why a manual request cannot be sent now."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"


class Status(StrEnum):
    """Where an endpoint stands."""

    OK = "ok"
    RETRYING = "retrying"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ON_DEMAND = "on_demand"


@final
@dataclass(frozen=True, slots=True)
class Budget:
    """A number of requests per period."""

    limit: int
    period: BudgetPeriod
    reserve_percent: int = DEFAULT_RESERVE_PERCENT

    @property
    def reserve(self) -> int:
        """Return how many requests are kept for manual use."""
        return math.ceil(self.limit * self.reserve_percent / 100)


@final
@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """User settings."""

    mode: UpdateMode = UpdateMode.AUTOMATIC
    interval: timedelta = DEFAULT_INTERVAL
    budget: Budget | None = None


@final
@dataclass(frozen=True, slots=True)
class Validators:
    """Values for a conditional request."""

    etag: str | None = field(default=None, repr=False)
    last_modified: str | None = None


@dataclass(slots=True)
class PolicyState:
    """Everything that must survive a restart."""

    used: int = 0
    period_start: datetime | None = None
    last_request: datetime | None = None
    backoff_until: datetime | None = None
    rate_limited_until: datetime | None = None
    failures: int = 0
    rate_limit_streak: int = 0
    auth_stopped: bool = False
    etag: str | None = field(default=None, repr=False)
    last_modified: str | None = None

    def to_data(self) -> dict[str, JsonValue]:
        """Return plain data for storage."""
        return {
            "used": self.used,
            "period_start": _time_text(self.period_start),
            "last_request": _time_text(self.last_request),
            "backoff_until": _time_text(self.backoff_until),
            "rate_limited_until": _time_text(self.rate_limited_until),
            "failures": self.failures,
            "rate_limit_streak": self.rate_limit_streak,
            "auth_stopped": self.auth_stopped,
            "etag": self.etag,
            "last_modified": self.last_modified,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, JsonValue]) -> PolicyState:
        """Read plain data. Anything malformed falls back to its default."""
        return cls(
            used=_int_value(data.get("used")),
            period_start=_time_value(data.get("period_start")),
            last_request=_time_value(data.get("last_request")),
            backoff_until=_time_value(data.get("backoff_until")),
            rate_limited_until=_time_value(data.get("rate_limited_until")),
            failures=_int_value(data.get("failures")),
            rate_limit_streak=_int_value(data.get("rate_limit_streak")),
            auth_stopped=data.get("auth_stopped") is True,
            etag=_text_value(data.get("etag")),
            last_modified=_text_value(data.get("last_modified")),
        )


def _time_text(value: datetime | None) -> str | None:
    """Write a time."""
    return value.isoformat() if value is not None else None


def _time_value(value: JsonValue) -> datetime | None:
    """Read a time."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _int_value(value: JsonValue) -> int:
    """Read a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text_value(value: JsonValue) -> str | None:
    """Read optional text."""
    return value if isinstance(value, str) else None


def parse_retry_after(value: str | None, now: datetime) -> timedelta | None:
    """Read `Retry-After` as seconds or an HTTP date, limited to 24 hours."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        wait = timedelta(seconds=min(int(text), int(RETRY_AFTER_CEILING.total_seconds())))
    else:
        try:
            moment = parsedate_to_datetime(text)
        except (ValueError, TypeError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        wait = moment - now
    if wait <= timedelta(0):
        return None
    return min(wait, RETRY_AFTER_CEILING)


def period_bounds(period: BudgetPeriod, now: datetime) -> tuple[datetime, datetime]:
    """Return the start and end of the calendar period holding a time."""
    moment = now.astimezone(UTC)
    if period is BudgetPeriod.HOUR:
        start = moment.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period is BudgetPeriod.DAY:
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == _MONTHS_IN_YEAR:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


@final
@dataclass(frozen=True, slots=True)
class ManualDecision:
    """Whether a manual request may be sent."""

    refusal: Refusal | None = None
    possible_at: datetime | None = None

    @property
    def allowed(self) -> bool:
        """Tell whether the request may go."""
        return self.refusal is None


@final
class RequestPolicy:
    """Pure decisions from settings, state, a clock and randomness."""

    def __init__(
        self,
        config: PolicyConfig,
        clock: Callable[[], datetime],
        random: Callable[[], float],
        state: PolicyState | None = None,
    ) -> None:
        """Use a clock and a source of numbers from 0 to 1 given by the caller."""
        self.config = config
        self.state = state if state is not None else PolicyState()
        self._clock = clock
        self._random = random

    def _roll_period(self) -> None:
        """Clear the count when its period has ended."""
        budget = self.config.budget
        state = self.state
        if budget is None:
            return
        start, _ = period_bounds(budget.period, self._clock())
        if state.period_start != start:
            state.period_start = start
            state.used = 0

    @property
    def used(self) -> int:
        """Return the requests counted in this period."""
        self._roll_period()
        return self.state.used

    @property
    def remaining(self) -> int | None:
        """Return the requests left, with the reserve, if a budget is set."""
        if self.config.budget is None:
            return None
        return max(self.config.budget.limit - self.used, 0)

    def _period_end(self) -> datetime | None:
        """Return when the budget renews."""
        budget = self.config.budget
        if budget is None:
            return None
        return period_bounds(budget.period, self._clock())[1]

    def _automatic_requests_left(self) -> int | None:
        """Return the requests left for automatic use, if a budget is set."""
        budget = self.config.budget
        if budget is None:
            return None
        return budget.limit - budget.reserve - self.used

    def spacing(self) -> timedelta:
        """Return the time between automatic requests."""
        left = self._automatic_requests_left()
        end = self._period_end()
        if left is None or end is None or left <= 0:
            return self.config.interval
        return max(self.config.interval, (end - self._clock()) / left)

    def next_request_at(self) -> datetime | None:
        """Return when the next automatic request is due, or `None` if never."""
        state = self.state
        if self.config.mode is UpdateMode.ON_DEMAND or state.auth_stopped:
            return None
        now = self._clock()
        moments = [now]
        if state.last_request is not None:
            moments.append(state.last_request + self.spacing())
        if state.backoff_until is not None:
            moments.append(state.backoff_until)
        if state.rate_limited_until is not None:
            moments.append(state.rate_limited_until)
        left = self._automatic_requests_left()
        end = self._period_end()
        if left is not None and left <= 0 and end is not None:
            moments.append(end)
        return max(moments)

    def is_due(self) -> bool:
        """Tell whether an automatic request may go now."""
        moment = self.next_request_at()
        return moment is not None and moment <= self._clock()

    def check_manual(self) -> ManualDecision:
        """Decide on a manual request."""
        now = self._clock()
        until = self.state.rate_limited_until
        if until is not None and until > now:
            return ManualDecision(Refusal.RATE_LIMITED, until)
        if self.remaining == 0:
            return ManualDecision(Refusal.BUDGET_EXHAUSTED, self._period_end())
        return ManualDecision()

    def record_sent(self) -> None:
        """Count a request. Call it for every request sent."""
        self._roll_period()
        self.state.used += 1
        self.state.last_request = self._clock()

    def validators(self) -> Validators | None:
        """Return validators for the next request, if any."""
        state = self.state
        if state.etag is None and state.last_modified is None:
            return None
        return Validators(state.etag, state.last_modified)

    def record_fetched(self, etag: str | None, last_modified: str | None) -> None:
        """Note a `200`."""
        self.state.etag = etag
        self.state.last_modified = last_modified
        self._clear_failures()

    def record_not_modified(self) -> None:
        """Note a `304`."""
        self._clear_failures()

    def _clear_failures(self) -> None:
        """End backoff."""
        state = self.state
        state.failures = 0
        state.rate_limit_streak = 0
        state.backoff_until = None
        state.rate_limited_until = None

    def _backoff(self) -> timedelta:
        """Count a failure and return the wait it causes."""
        state = self.state
        state.failures += 1
        spacing = self.spacing()
        ceiling = max(BACKOFF_CEILING, spacing)
        factor = 1 << min(state.failures, 32)
        wait = min(spacing * factor, ceiling)
        return wait * (1 + BACKOFF_VARIATION * (2 * self._random() - 1))

    def record_failure(self, retry_after: str | None = None) -> None:
        """Note a connection or server failure, or anything else unusable."""
        now = self._clock()
        wait = self._backoff()
        self.state.backoff_until = now + wait
        self.state.rate_limit_streak = 0
        requested = parse_retry_after(retry_after, now)
        if requested is not None:
            self.state.rate_limited_until = now + requested

    def record_rate_limited(self, retry_after: str | None) -> None:
        """Note a `429`, which also blocks manual requests."""
        now = self._clock()
        wait = self._backoff()
        requested = parse_retry_after(retry_after, now)
        if requested is not None:
            wait = max(wait, requested)
        self.state.backoff_until = now + wait
        self.state.rate_limited_until = now + wait
        self.state.rate_limit_streak += 1

    def record_auth_failed(self) -> None:
        """Stop automatic requests until credentials change."""
        self.state.auth_stopped = True

    def credentials_changed(self) -> None:
        """Resume after a change of URL or credentials. Used requests stay."""
        state = self.state
        state.auth_stopped = False
        state.etag = None
        state.last_modified = None
        self._clear_failures()

    def discard_validators(self) -> None:
        """Make the next request unconditional."""
        self.state.etag = None
        self.state.last_modified = None

    def status(self) -> Status:
        """Summarise where the endpoint stands."""
        state = self.state
        now = self._clock()
        if state.auth_stopped:
            return Status.AUTH_FAILED
        if state.rate_limited_until is not None and state.rate_limited_until > now:
            return Status.RATE_LIMITED
        left = self._automatic_requests_left()
        if left is not None and left <= 0 and self.config.mode is UpdateMode.AUTOMATIC:
            return Status.BUDGET_EXHAUSTED
        if self.remaining == 0:
            return Status.BUDGET_EXHAUSTED
        if state.failures > 0:
            return Status.RETRYING
        if self.config.mode is UpdateMode.ON_DEMAND:
            return Status.ON_DEMAND
        return Status.OK
