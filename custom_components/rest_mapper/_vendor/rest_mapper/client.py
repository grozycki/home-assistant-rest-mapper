"""One GET request with authentication, a size limit and typed outcomes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, final

import aiohttp
from yarl import URL

if TYPE_CHECKING:
    from .paths import JsonValue
    from .policy import Validators

DEFAULT_MAX_BYTES: Final = 2 * 1024 * 1024
MAX_MAX_BYTES: Final = 16 * 1024 * 1024
MIN_MAX_BYTES: Final = 64 * 1024
DEFAULT_TIMEOUT: Final = 15.0
MAX_REDIRECTS: Final = 5
_CHUNK_BYTES: Final = 64 * 1024
_OK: Final = 200
_NOT_MODIFIED: Final = 304
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_AUTH_FAILURES: Final = frozenset({401, 403})
_RATE_LIMITED: Final = 429
_SERVER_ERRORS_FROM: Final = 500


@final
@dataclass(frozen=True, slots=True)
class NoAuth:
    """Send no credentials."""


@final
@dataclass(frozen=True, slots=True)
class BearerAuth:
    """Send a Bearer token."""

    token: str = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class HeaderAuth:
    """Send a key in a named header."""

    name: str
    value: str = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class QueryAuth:
    """Send a key in a named query parameter."""

    name: str
    value: str = field(repr=False)


type Auth = NoAuth | BearerAuth | HeaderAuth | QueryAuth


@final
@dataclass(frozen=True, slots=True)
class FetchSpec:
    """What to request."""

    url: str = field(repr=False)
    auth: Auth = field(default_factory=NoAuth)
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout: float = DEFAULT_TIMEOUT


@final
@dataclass(frozen=True, slots=True)
class Fetched:
    """A `200` response."""

    body: bytes = field(repr=False)
    etag: str | None = None
    last_modified: str | None = None


@final
@dataclass(frozen=True, slots=True)
class NotModified:
    """A `304` response."""


class FetchError(Exception):
    """A request that gave no usable data. Its message holds no secrets."""


@final
class AuthFailedError(FetchError):
    """The endpoint refused the credentials."""

    def __init__(self, status: int) -> None:
        """Keep the status."""
        super().__init__(f"Refused with status {status}")
        self.status = status


@final
class RateLimitedError(FetchError):
    """The endpoint answered `429`."""

    def __init__(self, retry_after: str | None) -> None:
        """Keep the raw `Retry-After` value."""
        super().__init__("Rate limited")
        self.retry_after = retry_after


@final
class ServerFailedError(FetchError):
    """The endpoint answered with a `5xx` status."""

    def __init__(self, status: int, retry_after: str | None) -> None:
        """Keep the status and raw `Retry-After` value."""
        super().__init__(f"Server error {status}")
        self.status = status
        self.retry_after = retry_after


@final
class ClientFailedError(FetchError):
    """The endpoint answered with another unusable status."""

    def __init__(self, status: int) -> None:
        """Keep the status."""
        super().__init__(f"Unexpected status {status}")
        self.status = status


@final
class ConnectFailedError(FetchError):
    """The connection failed or timed out."""


@final
class RedirectError(FetchError):
    """A redirect left the origin, or there were too many."""


@final
class SizeExceededError(FetchError):
    """The body is larger than allowed."""


@final
class FormatError(FetchError):
    """The body is not JSON."""


def redact_url(url: str) -> str:
    """Return a URL with only its scheme, host, port and path."""
    try:
        parsed = URL(url)
    except ValueError:
        return "<invalid url>"
    if not parsed.host:
        return "<invalid url>"
    return str(parsed.with_user(None).with_query(None).with_fragment(None))


def _request_url(spec: FetchSpec) -> URL:
    """Return the URL with any key in its query."""
    url = URL(spec.url)
    if isinstance(spec.auth, QueryAuth):
        return url.update_query({spec.auth.name: spec.auth.value})
    return url


def _headers(spec: FetchSpec, validators: Validators | None) -> dict[str, str]:
    """Build request headers."""
    headers = {"Accept": "application/json"}
    match spec.auth:
        case BearerAuth():
            headers["Authorization"] = f"Bearer {spec.auth.token}"
        case HeaderAuth():
            headers[spec.auth.name] = spec.auth.value
        case NoAuth() | QueryAuth():
            pass
    if validators is not None:
        if validators.etag is not None:
            headers["If-None-Match"] = validators.etag
        if validators.last_modified is not None:
            headers["If-Modified-Since"] = validators.last_modified
    return headers


async def fetch(
    session: aiohttp.ClientSession,
    spec: FetchSpec,
    validators: Validators | None = None,
) -> Fetched | NotModified:
    """Send one GET. Raises a `FetchError` for anything but `200` and `304`."""
    url = _request_url(spec)
    headers = _headers(spec, validators)
    timeout = aiohttp.ClientTimeout(total=spec.timeout)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.get(
                url, headers=headers, timeout=timeout, allow_redirects=False
            ) as response:
                if response.status not in _REDIRECTS:
                    return await _read_response(response, spec.max_bytes)
                url = _follow(url, response.headers.get("Location"), spec)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        # The original error text can hold the full URL, so it is not chained.
        raise ConnectFailedError(f"{type(err).__name__} for {redact_url(spec.url)}") from None
    raise RedirectError("Too many redirects")


def _follow(current: URL, location: str | None, spec: FetchSpec) -> URL:
    """Return the redirect target, if it stays on the same origin."""
    if not location:
        raise RedirectError("Redirect without a location")
    target = current.join(URL(location))
    if (target.scheme, target.host, target.port) != (current.scheme, current.host, current.port):
        raise RedirectError("Redirect to another origin")
    if isinstance(spec.auth, QueryAuth):
        return target.update_query({spec.auth.name: spec.auth.value})
    return target


async def _read_response(response: aiohttp.ClientResponse, max_bytes: int) -> Fetched | NotModified:
    """Turn a final response into an outcome."""
    status = response.status
    if status == _OK:
        return Fetched(
            await _read_limited(response, max_bytes),
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
        )
    if status == _NOT_MODIFIED:
        return NotModified()
    if status in _AUTH_FAILURES:
        raise AuthFailedError(status)
    retry_after = response.headers.get("Retry-After")
    if status == _RATE_LIMITED:
        raise RateLimitedError(retry_after)
    if status >= _SERVER_ERRORS_FROM:
        raise ServerFailedError(status, retry_after)
    raise ClientFailedError(status)


async def _read_limited(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """Read a decompressed body, stopping at the limit."""
    declared = response.content_length
    if declared is not None and declared > max_bytes:
        raise SizeExceededError(f"Declared {declared} bytes")
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        body += chunk
        if len(body) > max_bytes:
            raise SizeExceededError(f"More than {max_bytes} bytes")
    return bytes(body)


def decode_json(body: bytes) -> JsonValue:
    """Decode a body as JSON. Run this outside an event loop for large bodies."""
    try:
        text = body.decode("utf-8-sig")
        decoded: JsonValue = json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise FormatError("The body is not JSON") from None
    return decoded


def _refuse_constant(_: str) -> None:
    """Refuse `NaN` and `Infinity`, which JSON does not allow."""
    raise json.JSONDecodeError("Not a JSON constant", "", 0)
