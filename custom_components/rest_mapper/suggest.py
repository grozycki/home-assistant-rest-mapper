"""Guess sensible settings from a path and its example value."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPressure, UnitOfSpeed, UnitOfTemperature

from .lib import ScalarType, ValueType, parse_path

if TYPE_CHECKING:
    from .lib import FlatField, JsonValue


@final
@dataclass(frozen=True, slots=True)
class Guess:
    """Suggested sensor settings."""

    device_class: SensorDeviceClass | None = None
    unit: str | None = None


_BY_NAME: Final[tuple[tuple[str, Guess], ...]] = (
    ("temp", Guess(SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS)),
    ("humidity", Guess(SensorDeviceClass.HUMIDITY, PERCENTAGE)),
    ("pressure", Guess(SensorDeviceClass.ATMOSPHERIC_PRESSURE, UnitOfPressure.HPA)),
    ("wind", Guess(SensorDeviceClass.WIND_SPEED, UnitOfSpeed.METERS_PER_SECOND)),
    ("battery", Guess(SensorDeviceClass.BATTERY, PERCENTAGE)),
)


def last_key(path: str, *, relative: bool = False) -> str:
    """Return the last named part of a path, for suggesting a name."""
    for segment in reversed(parse_path(path, relative=relative).segments):
        if isinstance(segment, str):
            return segment
    return "value"


def guess_sensor(path: str, *, relative: bool = False) -> Guess:
    """Guess a sensor from its path's name."""
    name = last_key(path, relative=relative).casefold()
    for marker, guess in _BY_NAME:
        if marker in name:
            return guess
    return Guess()


def value_type_of(field: FlatField | None) -> ValueType:
    """Guess how a field is read."""
    if field is None or field.type is ScalarType.NUMBER:
        return ValueType.NUMBER
    if field.type is ScalarType.BOOLEAN:
        return ValueType.BOOLEAN
    return ValueType.TEXT


def settings_for_field(field: FlatField) -> dict[str, JsonValue]:
    """Return settings for a sensor picked in bulk."""
    guess = guess_sensor(field.path)
    settings: dict[str, JsonValue] = {"value_type": value_type_of(field).value}
    if field.type is ScalarType.NUMBER:
        settings["state_class"] = SensorStateClass.MEASUREMENT.value
        if guess.device_class is not None:
            settings["device_class"] = guess.device_class.value
        if guess.unit is not None:
            settings["unit_of_measurement"] = guess.unit
    return settings
