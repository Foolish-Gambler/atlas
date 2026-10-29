from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from atlas.ingest.edgar import EdgarQuery
from atlas.ingest.edgar import adapter_for_tests as edgar_adapter
from atlas.ingest.filing_docs import (
    RETRIEVAL_VENDOR_GET,
    FilingDocumentError,
    FilingDocumentQuery,
    adapter_for_tests,
)
from atlas.ingest.http import (
    SEC_WWW_HOST,
    ReadOnlyHttp,
    ReadOnlyHttpError,
    format_http_failure,
    sanitize_http_text,
)
from atlas.ingest.snapshot import SnapshotStore

UA = "ATLAS tests contact@example.com"
URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"
BODY = b"<html><body><p>Net sales 94036</p></body></html>"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ingest" / "edgar"


@pytest.fixture
def edgar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_EDGAR_USER_AGENT", UA)


def _source(tmp_path: Path):
    pages = json.loads((FIXTURE_DIR / "aapl_pages.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json=pages["tickers"])
        if "/submissions/" in path:
            return httpx.Response(200, json=pages["submissions"])
        if "/companyfacts/" in path:
            return httpx.Response(200, json=pages["companyfacts"])
        return httpx.Response(404, json={"error": path})

    return edgar_adapter(handler).ingest(
        SnapshotStore(tmp_path),
        dataset_id="edgar-aapl",
        revision="meta-1",
        query=EdgarQuery(symbol="AAPL"),
    )


def _client(
    handler: object,
    *,
    max_retries: int = 3,
    sleeper: object | None = None,
) -> tuple[ReadOnlyHttp, list[float]]:
    delays: list[float] = []
    http = ReadOnlyHttp(
        allowed_hosts=frozenset({SEC_WWW_HOST}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_network=False,
        rate_limit_sleep=0.0,
        max_retries=max_retries,
        sleeper=sleeper or delays.append,
        user_agent=UA,
    )
    return http, delays


def test_sanitize_http_text_redacts_user_agent_and_secrets() -> None:
    raw = (
        f"User-Agent: {UA}; Authorization: Bearer secret; "
        "Proxy-Authorization: Basic abc; Cookie: sid=1"
    )
    cleaned = sanitize_http_text(raw, user_agent=UA)
    assert UA not in cleaned
    assert "Bearer secret" not in cleaned
    assert "Basic abc" not in cleaned
    assert "sid=1" not in cleaned
    assert "User-Agent: <redacted>" in cleaned


def test_streaming_200_without_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY, headers={"content-type": "text/html"})

    http, delays = _client(handler)
    response = http.get_bounded(URL, max_bytes=8_000_000)
    assert response.status_code == 200
    assert response.content == BODY
    assert delays == []


def test_read_timeout_preserves_cause_and_attempts() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    http, _ = _client(handler)
    with pytest.raises(ReadOnlyHttpError, match="ReadTimeout") as caught:
        http.get_bounded(URL, max_bytes=8_000_000)
    error = caught.value
    assert calls["n"] == 3
    assert error.attempts == 3
    assert error.exception_type == "ReadTimeout"
    assert error.status_code is None
    assert isinstance(error.__cause__, httpx.ReadTimeout)
    assert UA not in str(error)
    assert UA not in format_http_failure(error, user_agent=UA)


def test_connect_error_is_retryable_and_chained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http, _ = _client(handler, max_retries=2)
    with pytest.raises(ReadOnlyHttpError, match="ConnectError") as caught:
        http.get_bounded(URL, max_bytes=1024)
    assert caught.value.attempts == 2
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


def test_remote_protocol_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    http, _ = _client(handler)
    with pytest.raises(ReadOnlyHttpError, match="RemoteProtocolError") as caught:
        http.get_bounded(URL, max_bytes=1024)
    assert caught.value.exception_type == "RemoteProtocolError"
    assert caught.value.attempts == 3


def test_http_429_honors_capped_retry_after() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "120"}, content=b"slow")
        return httpx.Response(200, content=BODY, headers={"content-type": "text/html"})

    delays: list[float] = []
    http, _ = _client(handler, sleeper=delays.append)
    response = http.get_bounded(URL, max_bytes=8_000_000)
    assert response.content == BODY
    assert state["n"] == 2
    assert delays == [8.0]


def test_http_503_then_success() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, content=BODY, headers={"content-type": "text/html"})

    http, _ = _client(handler)
    response = http.get_bounded(URL, max_bytes=8_000_000)
    assert state["n"] == 2
    assert response.status_code == 200
    assert response.content == BODY


def test_http_403_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, content=b"forbidden")

    delays: list[float] = []
    http, _ = _client(handler, sleeper=delays.append)
    with pytest.raises(ReadOnlyHttpError, match="HTTP 403") as caught:
        http.get_bounded(URL, max_bytes=8_000_000)
    assert calls["n"] == 1
    assert delays == []
    assert caught.value.status_code == 403
    assert caught.value.attempts == 1
    assert caught.value.retryable is False


def test_on_attempt_limits_actual_http_calls() -> None:
    calls = {"http": 0, "budget": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["http"] += 1
        return httpx.Response(503, content=b"unavailable")

    def on_attempt() -> None:
        calls["budget"] += 1
        if calls["budget"] > 2:
            raise FilingDocumentError("document ingest exceeded max_requests=2.")

    http, _ = _client(handler, max_retries=5)
    with pytest.raises(FilingDocumentError, match="max_requests=2"):
        http.get_bounded(URL, max_bytes=8_000_000, on_attempt=on_attempt)
    assert calls["http"] == 2
    assert calls["budget"] == 3


def test_adapter_failure_does_not_publish_snapshot(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(FilingDocumentError, match="ReadTimeout") as caught:
        adapter_for_tests(handler, max_retries=3).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="timeout",
            query=FilingDocumentQuery(
                accession="0000320193-26-000020",
                max_requests=8,
                require_success=True,
            ),
            retrieval_mode=RETRIEVAL_VENDOR_GET,
        )
    assert not store.revision_path("edgar-docs", "timeout").exists()
    assert calls["n"] == 3
    message = str(caught.value)
    assert "0000320193-26-000020" in message
    assert "ReadTimeout" in message
    assert "attempts=3" in message or "after 3 attempts" in message
    assert UA not in message
    assert caught.value.__cause__ is not None


def test_adapter_max_requests_counts_retries(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"unavailable")

    with pytest.raises(FilingDocumentError, match="max_requests=2"):
        adapter_for_tests(handler, max_retries=5).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="budget",
            query=FilingDocumentQuery(
                accession="0000320193-26-000020",
                max_requests=2,
                require_success=True,
            ),
            retrieval_mode=RETRIEVAL_VENDOR_GET,
        )
    assert calls["n"] == 2
    assert not store.revision_path("edgar-docs", "budget").exists()
