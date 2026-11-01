"""Address values inside a decoded JSON document."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, final, override

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type Segment = str | int

ROOT: Final = "$"
_PLAIN_KEY: Final = re.compile(r"[^.\[\]'\"*?()@,:\s$]+")


class Missing(Enum):
    """Marks a value that is absent, as opposed to a JSON null."""

    MISSING = "missing"


MISSING: Final = Missing.MISSING


@final
class PathError(ValueError):
    """A path has invalid syntax."""

    def __init__(self, text: str, position: int) -> None:
        """Keep where in the text parsing stopped."""
        super().__init__(f"Invalid path {text!r} at position {position}")
        self.text = text
        self.position = position


@final
@dataclass(frozen=True, slots=True)
class Path:
    """A parsed path, absolute from the root or relative to a list item."""

    segments: tuple[Segment, ...]
    relative: bool = False

    @override
    def __str__(self) -> str:
        """Return the canonical text form."""
        return format_path(self.segments, relative=self.relative)


def format_path(segments: tuple[Segment, ...], *, relative: bool = False) -> str:
    """Write segments in the canonical text form."""
    text = "" if relative else ROOT
    for segment in segments:
        if isinstance(segment, int):
            text += f"[{segment}]"
        elif _PLAIN_KEY.fullmatch(segment):
            text += f".{segment}" if text else segment
        else:
            escaped = segment.replace("\\", "\\\\").replace("'", "\\'")
            text += f"['{escaped}']"
    return text


def parse_path(text: str, *, relative: bool = False) -> Path:
    """Parse a path, raising `PathError` for anything outside the syntax.

    A relative path addresses a value inside a list item and has no `$`.
    """
    position = 0
    segments: list[Segment] = []
    if relative:
        if not text:
            raise PathError(text, 0)
        if text[0] != "[":
            key, position = _read_plain_key(text, 0)
            segments.append(key)
    else:
        if not text.startswith(ROOT):
            raise PathError(text, 0)
        position = 1

    while position < len(text):
        character = text[position]
        if character == ".":
            key, position = _read_plain_key(text, position + 1)
            segments.append(key)
        elif character == "[":
            segment, position = _read_bracket(text, position + 1)
            segments.append(segment)
        else:
            raise PathError(text, position)
    return Path(tuple(segments), relative)


def _read_plain_key(text: str, position: int) -> tuple[str, int]:
    """Read an unquoted key."""
    match = _PLAIN_KEY.match(text, position)
    if match is None:
        raise PathError(text, position)
    return match.group(), match.end()


def _read_bracket(text: str, position: int) -> tuple[Segment, int]:
    """Read what follows an opening bracket, up to and past its closing one."""
    if position >= len(text):
        raise PathError(text, position)
    if text[position] in "'\"":
        return _read_quoted_key(text, position)
    end = text.find("]", position)
    index = text[position:end] if end != -1 else ""
    if not (index.isascii() and index.isdigit()):
        raise PathError(text, position)
    return int(index), end + 1


def _read_quoted_key(text: str, position: int) -> tuple[str, int]:
    """Read a quoted key, with backslash escapes, and its closing bracket."""
    quote = text[position]
    key = ""
    current = position + 1
    while current < len(text):
        character = text[current]
        if character == "\\" and current + 1 < len(text):
            key += text[current + 1]
            current += 2
        elif character == quote:
            if text[current + 1 : current + 2] != "]":
                raise PathError(text, current + 1)
            return key, current + 2
        else:
            key += character
            current += 1
    raise PathError(text, position)


def resolve(data: JsonValue, path: Path) -> JsonValue | Missing:
    """Return the value at a path, or `MISSING` when there is none."""
    current = data
    for segment in path.segments:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return MISSING
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return MISSING
            current = current[segment]
    return current
