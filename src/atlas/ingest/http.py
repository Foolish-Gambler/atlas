"""Read-only HTTP helper. Network is off unless ATLAS_ALLOW_NETWORK is set."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

ALPACA_DATA_HOST = "data.alpaca.markets"
SEC_WWW_HOST = "www.sec.gov"
SEC_DATA_HOST = "data.sec.gov"
SEC_ALLOWED_HOSTS = frozenset({SEC_WWW_HOST, SEC_DATA_HOST})
_BLOCKED_HOSTS = frozenset({"api.alpaca.markets", "paper-api.alpaca.markets"})


class NetworkDisabledError(RuntimeError):
    """Raised when a vendor GET is attempted without an explicit network grant."""


class ReadOnlyHttpError(RuntimeError):
    """Vendor HTTP failed after retries or returned a non-success status."""


def network_allowed() -> bool:
    return os.environ.get("ATLAS_ALLOW_NETWORK", "").strip().lower() in {"1", "true", "yes"}


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
        headers = {"User-Agent": user_agent or "ATLAS-read-only/0.1"}
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers=headers,
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
                self._sleeper(min(2**attempt, 8))
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self._sleeper(delay)
                last_error = ReadOnlyHttpError(f"HTTP {response.status_code} from {parsed.path}")
                continue
            if response.status_code >= 400:
                raise ReadOnlyHttpError(f"HTTP {response.status_code} from {parsed.path}")
            return response
        raise ReadOnlyHttpError(f"GET failed after {self.max_retries} attempts") from last_error

    def get_bounded(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
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
        for attempt in range(self.max_retries):
            if self.rate_limit_sleep and attempt == 0:
                self._sleeper(self.rate_limit_sleep)
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers=dict(headers or {}),
                    timeout=self.timeout_seconds,
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = (
                                float(retry_after) if retry_after is not None else float(2**attempt)
                            )
                        except ValueError:
                            delay = float(2**attempt)
                        self._sleeper(delay)
                        last_error = ReadOnlyHttpError(
                            f"HTTP {response.status_code} from {parsed.path}"
                        )
                        continue
                    if response.status_code >= 400:
                        raise ReadOnlyHttpError(f"HTTP {response.status_code} from {parsed.path}")
                    if 300 <= response.status_code < 400:
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=b"",
                            request=response.request,
                        )
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
                            raise ReadOnlyHttpError("response exceeds max_bytes.")
                        received.extend(part)
                    bounded = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=bytes(received),
                        request=response.request,
                    )
                    return bounded
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleeper(min(2**attempt, 8))
                continue
        raise ReadOnlyHttpError(f"GET failed after {self.max_retries} attempts") from last_error
