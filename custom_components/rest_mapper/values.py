"""Values read for entities, and their stored form."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import final

from .lib import Failure, JsonValue, Position


@final
@dataclass(frozen=True, slots=True)
class EventData:
    """One calendar event."""

    start: datetime | date
    end: datetime | date | None
    summary: str
    description: str | None = None
    location: str | None = None


type ReadValue = int | float | str | bool | datetime | date | Position | EventData | None


@final
@dataclass(slots=True)
class ValueRead:
    """What is known about one mapping or list item."""

    value: ReadValue = None
    confirmed_at: datetime | None = None
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    failures: int = 0
    failure: Failure | None = None
    name: str | None = None

    def to_data(self) -> dict[str, JsonValue]:
        """Return plain data for storage."""
        return {
            "value": encode_value(self.value),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "attributes": self.attributes,
            "failures": self.failures,
            "failure": self.failure.value if self.failure else None,
            "name": self.name,
        }

    @classmethod
    def from_data(cls, data: JsonValue) -> ValueRead:
        """Read plain data. Anything malformed gives an empty read."""
        if not isinstance(data, dict):
            return cls()
        attributes = data.get("attributes")
        failures = data.get("failures")
        failure = data.get("failure")
        name = data.get("name")
        return cls(
            value=decode_value(data.get("value")),
            confirmed_at=_time(data.get("confirmed_at")),
            attributes=attributes if isinstance(attributes, dict) else {},
            failures=failures if isinstance(failures, int) else 0,
            failure=Failure(failure) if failure in tuple(Failure) else None,
            name=name if isinstance(name, str) else None,
        )


def _time(value: JsonValue) -> datetime | None:
    """Read a time."""
    moment = _moment(value)
    return moment if isinstance(moment, datetime) else None


def _moment(value: JsonValue) -> datetime | date | None:
    """Read a time or a date."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    except ValueError:
        return None


def encode_value(value: ReadValue) -> JsonValue:
    """Write a value, tagging what JSON cannot hold."""
    if isinstance(value, Position):
        return {
            "$": "position",
            "latitude": value.latitude,
            "longitude": value.longitude,
            "accuracy": value.accuracy,
        }
    if isinstance(value, EventData):
        return {
            "$": "event",
            "start": value.start.isoformat(),
            "end": value.end.isoformat() if value.end is not None else None,
            "summary": value.summary,
            "description": value.description,
            "location": value.location,
        }
    if isinstance(value, date):
        return {"$": "moment", "value": value.isoformat()}
    return value


def decode_value(data: JsonValue) -> ReadValue:
    """Read a value written by `encode_value`."""
    if isinstance(data, list):
        return None
    if not isinstance(data, dict):
        return data
    match data.get("$"):
        case "moment":
            return _moment(data.get("value"))
        case "position":
            return _decode_position(data)
        case "event":
            return _decode_event(data)
        case _:
            return None


def _decode_position(data: dict[str, JsonValue]) -> Position | None:
    """Read a stored position."""
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    accuracy = data.get("accuracy")
    if not isinstance(latitude, int | float) or not isinstance(longitude, int | float):
        return None
    return Position(latitude, longitude, accuracy if isinstance(accuracy, int | float) else 0.0)


def _decode_event(data: dict[str, JsonValue]) -> EventData | None:
    """Read a stored event."""
    start = _moment(data.get("start"))
    summary = data.get("summary")
    if start is None or not isinstance(summary, str):
        return None
    description = data.get("description")
    location = data.get("location")
    return EventData(
        start,
        _moment(data.get("end")),
        summary,
        description if isinstance(description, str) else None,
        location if isinstance(location, str) else None,
    )
