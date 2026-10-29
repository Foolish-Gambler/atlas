"""Read-only HTTP helper. Network is off unless ATLAS_ALLOW_NETWORK is set."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

ALPACA_DATA_HOST = "data.alpaca.markets"
SEC_WWW_HOST = "www.sec.gov"
SEC_DATA_HOST = "data.sec.gov"
SEC_ALLOWED_HOSTS = frozenset({SEC_WWW_HOST, SEC_DATA_HOST})
_BLOCKED_HOSTS = frozenset({"api.alpaca.markets", "paper-api.alpaca.markets"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_WAIT_SECONDS = 8.0
_REDACT_HEADER_RE = re.compile(
    r"(?i)(authorization|proxy-authorization|cookie|set-cookie|x-api-key|user-agent)"
    r"\s*[:=]\s*[^\r\n;]+"
)


class NetworkDisabledError(RuntimeError):
    """Raised when a vendor GET is attempted without an explicit network grant."""


class ReadOnlyHttpError(RuntimeError):
    """Vendor HTTP failed after retries or returned a non-success status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        exception_type: str | None = None,
        attempts: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.exception_type = exception_type
        self.attempts = attempts
        self.retryable = retryable


def network_allowed() -> bool:
    return os.environ.get("ATLAS_ALLOW_NETWORK", "").strip().lower() in {"1", "true", "yes"}


def sanitize_http_text(text: str, *, user_agent: str | None = None) -> str:
    """Remove credentials, proxy auth, cookies, and User-Agent contact details from error text."""
    redacted = text
    if user_agent:
        redacted = redacted.replace(user_agent, "<redacted-user-agent>")
    return _REDACT_HEADER_RE.sub(r"\1: <redacted>", redacted)


def format_http_failure(exc: BaseException, *, user_agent: str | None = None) -> str:
    parts = [sanitize_http_text(str(exc) or type(exc).__name__, user_agent=user_agent)]
    if isinstance(exc, ReadOnlyHttpError):
        extras: list[str] = []
        if exc.status_code is not None and f"status={exc.status_code}" not in parts[0]:
            extras.append(f"status={exc.status_code}")
        if exc.exception_type and exc.exception_type not in parts[0]:
            extras.append(f"cause={exc.exception_type}")
        if exc.attempts and f"after {exc.attempts} attempt" not in parts[0]:
            extras.append(f"attempts={exc.attempts}")
        if extras:
            parts.append("(" + ", ".join(extras) + ")")
    cause = exc.__cause__
    if cause is not None:
        cause_text = (
            f"{type(cause).__name__}: {sanitize_http_text(str(cause), user_agent=user_agent)}"
        )
        if cause_text not in parts[0]:
            parts.append(f"caused by {cause_text}")
    return " ".join(parts)


class ReadOnlyHttp:
    """GET-only client bound to a single host. Never logs Authorization headers."""

    def __init__(
        self,
        *,
        allowed_host: str | None = None,
        allowed_hosts: frozenset[str] | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        rate_limit_sleep: float = 0.2,
        client: httpx.Client | None = None,
        allow_network: bool | None = None,
        sleeper: Any = None,
        user_agent: str | None = None,
    ) -> None:
        hosts = set(allowed_hosts or ())
        if allowed_host:
            hosts.add(allowed_host)
        if not hosts:
            raise ValueError("allowed_host or allowed_hosts is required.")
        self.allowed_hosts = frozenset(hosts)
        self.allowed_host = allowed_host or sorted(hosts)[0]
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.rate_limit_sleep = rate_limit_sleep
        self._sleeper = sleeper or __import__("time").sleep
        granted = network_allowed() if allow_network is None else allow_network
        if client is None and not granted:
            raise NetworkDisabledError(
                "Vendor HTTP is disabled. Set ATLAS_ALLOW_NETWORK=1 only after explicit approval."
            )
        self._owns_client = client is None
        self._user_agent = user_agent or "ATLAS-read-only/0.1"
        headers = {"User-Agent": self._user_agent}
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers=headers,
            verify=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lower()
        if host in _BLOCKED_HOSTS or "/orders" in path or "/v2/account" in path:
            raise ReadOnlyHttpError("trading and account endpoints are forbidden.")
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise ReadOnlyHttpError(f"GET host {host!r} is not allowed.")
        last_error: Exception | None = None
        last_status: int | None = None
        for attempt in range(self.max_retries):
            if self.rate_limit_sleep and attempt == 0:
                self._sleeper(self.rate_limit_sleep)
            try:
                response = self._client.request(
                    "GET",
                    url,
                    params=dict(params or {}),
                    headers=dict(headers or {}),
                    timeout=self.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if not _retryable_transport_error(exc) or attempt + 1 >= self.max_retries:
                    raise self._exhausted_error(
                        parsed.path, attempt + 1, last_error, last_status
                    ) from exc
                self._sleeper(_retry_wait_seconds(None, attempt))
                continue
            last_status = response.status_code
            if response.status_code in _RETRYABLE_STATUS:
                retry_after = response.headers.get("Retry-After")
                last_error = ReadOnlyHttpError(
                    f"HTTP {response.status_code} from {parsed.path}",
                    status_code=response.status_code,
                    exception_type="HTTPStatus",
                    attempts=attempt + 1,
                    retryable=True,
                )
                _close_quietly(response)
                if attempt + 1 >= self.max_retries:
                    break
                self._sleeper(_retry_wait_seconds(retry_after, attempt))
                continue
            if response.status_code >= 400:
                raise ReadOnlyHttpError(
                    f"HTTP {response.status_code} from {parsed.path} "
                    f"after {attempt + 1} attempt(s)",
                    status_code=response.status_code,
                    exception_type="HTTPStatus",
                    attempts=attempt + 1,
                )
            return response
        raise self._exhausted_error(
            parsed.path, self.max_retries, last_error, last_status
        ) from last_error

    def get_bounded(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        on_attempt: Callable[[], None] | None = None,
    ) -> httpx.Response:
        """GET a body of at most max_bytes. Extra bytes are neither retained nor returned."""
        if max_bytes < 1:
            raise ReadOnlyHttpError("max_bytes must be positive.")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lower()
        if host in _BLOCKED_HOSTS or "/orders" in path or "/v2/account" in path:
            raise ReadOnlyHttpError("trading and account endpoints are forbidden.")
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise ReadOnlyHttpError(f"GET host {host!r} is not allowed.")
        last_error: Exception | None = None
        last_status: int | None = None
        attempts = 0
        for attempt in range(self.max_retries):
            if on_attempt is not None:
                on_attempt()
            attempts += 1
            if self.rate_limit_sleep and attempts == 1:
                self._sleeper(self.rate_limit_sleep)
            retry_after: str | None = None
            retry_request = False
            body: bytes | None = None
            response_headers: httpx.Headers | None = None
            response_request: httpx.Request | None = None
            status_code = 0
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers=dict(headers or {}),
                    timeout=self.timeout_seconds,
                ) as response:
                    status_code = response.status_code
                    last_status = status_code
                    if status_code in _RETRYABLE_STATUS:
                        retry_after = response.headers.get("Retry-After")
                        last_error = ReadOnlyHttpError(
                            f"HTTP {status_code} from {parsed.path}",
                            status_code=status_code,
                            exception_type="HTTPStatus",
                            attempts=attempts,
                            retryable=True,
                        )
                        retry_request = True
                    elif status_code >= 400:
                        raise ReadOnlyHttpError(
                            f"HTTP {status_code} from {parsed.path} after {attempts} attempt(s)",
                            status_code=status_code,
                            exception_type="HTTPStatus",
                            attempts=attempts,
                        )
                    elif 300 <= status_code < 400:
                        body = b""
                        response_headers = response.headers
                        response_request = response.request
                    else:
                        length = response.headers.get("Content-Length")
                        if length:
                            try:
                                declared = int(length)
                            except ValueError as exc:
                                raise ReadOnlyHttpError("invalid Content-Length.") from exc
                            if declared > max_bytes:
                                raise ReadOnlyHttpError("Content-Length exceeds max_bytes.")
                        received = bytearray()
                        for part in response.iter_bytes(64 * 1024):
                            if not part:
                                continue
                            if len(received) + len(part) > max_bytes:
                                received.clear()
                                raise ReadOnlyHttpError("response exceeds max_bytes.")
                            received.extend(part)
                        body = bytes(received)
                        response_headers = response.headers
                        response_request = response.request
            except ReadOnlyHttpError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if not _retryable_transport_error(exc) or attempts >= self.max_retries:
                    raise self._exhausted_error(
                        parsed.path, attempts, last_error, last_status
                    ) from exc
                self._sleeper(_retry_wait_seconds(None, attempt))
                continue
            if retry_request:
                if attempts >= self.max_retries:
                    break
                self._sleeper(_retry_wait_seconds(retry_after, attempt))
                continue
            return httpx.Response(
                status_code,
                headers=response_headers,
                content=body or b"",
                request=response_request,
            )
        raise self._exhausted_error(parsed.path, attempts, last_error, last_status) from last_error

    def _exhausted_error(
        self,
        path: str,
        attempts: int,
        last_error: Exception | None,
        last_status: int | None = None,
    ) -> ReadOnlyHttpError:
        cause_name = type(last_error).__name__ if last_error is not None else "unknown"
        if isinstance(last_error, ReadOnlyHttpError) and last_error.exception_type:
            cause_name = last_error.exception_type
        status_label = f"status={last_status}" if last_status is not None else "status=none"
        cause_text = ""
        if last_error is not None:
            cause_text = sanitize_http_text(str(last_error), user_agent=self._user_agent)
        message = (
            f"GET failed after {attempts} attempts ({status_label}, cause={cause_name}"
            + (f": {cause_text}" if cause_text else "")
            + f", path={path})"
        )
        status_code = last_status
        if isinstance(last_error, ReadOnlyHttpError) and last_error.status_code is not None:
            status_code = last_error.status_code
        return ReadOnlyHttpError(
            message,
            status_code=status_code,
            exception_type=cause_name,
            attempts=attempts,
            retryable=True,
        )


def _close_quietly(response: httpx.Response) -> None:
    try:
        response.close()
    except httpx.HTTPError:
        return


def _retryable_transport_error(exc: httpx.HTTPError) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ),
    )


def _retry_wait_seconds(retry_after: str | None, attempt_index: int) -> float:
    backoff = min(float(2**attempt_index), _MAX_RETRY_WAIT_SECONDS)
    if retry_after is None:
        return backoff
    try:
        parsed = float(retry_after.strip())
    except ValueError:
        return backoff
    if parsed < 0:
        return backoff
    return min(parsed, _MAX_RETRY_WAIT_SECONDS)
