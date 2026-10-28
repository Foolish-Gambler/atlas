from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from atlas.backtesting.view import build_view
from atlas.contracts import EventClock
from atlas.ingest.edgar import (
    EdgarIntegrityError,
    EdgarQuery,
    MissingEdgarUserAgentError,
    adapter_for_tests,
    load_edgar_user_agent,
)
from atlas.ingest.http import ReadOnlyHttp, ReadOnlyHttpError
from atlas.ingest.snapshot import RevisionExistsError, SnapshotStore

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ingest" / "edgar" / "aapl_pages.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture
def edgar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_EDGAR_USER_AGENT", "ATLAS tests contact@example.com")


def _handler(log: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        if request.method != "GET":
            return httpx.Response(405, json={"error": "GET only"})
        path = request.url.path
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json=FIXTURE["tickers"])
        if "/submissions/" in path:
            return httpx.Response(200, json=FIXTURE["submissions"])
        if "/companyfacts/" in path:
            return httpx.Response(200, json=FIXTURE["companyfacts"])
        return httpx.Response(404, json={"error": path})

    return handler


def test_missing_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(MissingEdgarUserAgentError, match="ATLAS_EDGAR_USER_AGENT"):
        load_edgar_user_agent()


def test_ticker_cik_filings_and_xbrl_roundtrip(edgar_env: None, tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    store = SnapshotStore(tmp_path)
    result = adapter_for_tests(_handler(log)).ingest(
        store,
        dataset_id="edgar-aapl",
        revision="mock-1",
        query=EdgarQuery(symbol="AAPL"),
    )
    assert all(request.method == "GET" for request in log)
    hosts = {request.url.host for request in log}
    assert hosts <= {"www.sec.gov", "data.sec.gov"}
    assert any(request.url.path.endswith("company_tickers.json") for request in log)
    assert any("/submissions/CIK0000320193.json" in request.url.path for request in log)
    assert any("/companyfacts/CIK0000320193.json" in request.url.path for request in log)
    assert result.cik == "0000320193"
    assert result.simulated is True
    assert result.authenticated_vendor_data is False
    forms = {filing.form_type for filing in result.filings}
    assert forms == {"8-K", "10-Q", "10-K", "10-K/A"}
    assert "4" not in forms
    eight_k = next(item for item in result.filings if item.form_type == "8-K")
    assert eight_k.time.event_time == eight_k.time.available_time
    assert eight_k.items == ("2.02", "9.01")
    ten_k = next(item for item in result.filings if item.form_type == "10-K")
    assert ten_k.time.event_time.date().isoformat() == "2023-09-30"
    assert ten_k.time.available_time == datetime(2023, 11, 3, 18, 4, 28, tzinfo=UTC)
    assert ten_k.time.available_time > ten_k.time.event_time
    revenues = [fact for fact in result.facts if fact.concept == "Revenues"]
    assert len(revenues) == 2
    assert {item.accession for item in revenues} == {
        "0000320193-23-000106",
        "0000320193-23-000077",
    }
    assert result.statements
    statement = result.statements[0]
    assert statement.earnings_per_share == 6.13
    assert statement.book_value_per_share == pytest.approx(62146000000 / 15550061000)
    assert dict(statement.metrics)["revenue"] == 383285000000
    loaded = store.load("edgar-aapl", "mock-1")
    assert loaded.manifest.metadata["retrieval_mode"] == "mocked_http"
    assert loaded.manifest.vendor == "sec_edgar"
    assert loaded.facts()
    assert "vendor/raw_bars.jsonl" in loaded.manifest.files
    assert loaded.vendor_raw()
    assert "contact@example.com" not in json.dumps(loaded.manifest.to_dict())
    clock = EventClock(loaded.stream())
    as_of = datetime(2023, 11, 4, tzinfo=UTC)
    view = build_view(loaded, clock.visible(as_of), as_of)
    assert any(item.form_type == "10-K" for item in view.filings)
    assert not any(item.form_type == "10-K/A" for item in view.filings)
    later = datetime(2023, 12, 16, tzinfo=UTC)
    restated = build_view(loaded, clock.visible(later), later)
    assert any(item.form_type == "10-K/A" for item in restated.filings)
    assert len([fact for fact in restated.facts if fact.concept == "Revenues"]) == 2
    with pytest.raises(RevisionExistsError):
        adapter_for_tests(_handler()).ingest(
            store,
            dataset_id="edgar-aapl",
            revision="mock-1",
            query=EdgarQuery(symbol="AAPL"),
        )


def test_unknown_ticker_and_request_bound(edgar_env: None, tmp_path: Path) -> None:
    with pytest.raises(EdgarIntegrityError, match="ticker"):
        adapter_for_tests(_handler()).ingest(
            SnapshotStore(tmp_path),
            dataset_id="missing",
            revision="1",
            query=EdgarQuery(symbol="ZZZZ"),
        )
    with pytest.raises(EdgarIntegrityError, match="max_requests"):
        adapter_for_tests(_handler()).ingest(
            SnapshotStore(tmp_path),
            dataset_id="bounded",
            revision="1",
            query=EdgarQuery(symbol="AAPL", max_requests=1),
        )
    assert not (tmp_path / "missing" / "1").exists()
    assert not (tmp_path / "bounded" / "1").exists()


def test_sec_hosts_only_and_retries(edgar_env: None) -> None:
    http = ReadOnlyHttp(
        allowed_hosts=frozenset({"www.sec.gov", "data.sec.gov"}),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        allow_network=False,
        rate_limit_sleep=0.0,
        sleeper=lambda _delay: None,
        user_agent="ATLAS tests contact@example.com",
    )
    with pytest.raises(ReadOnlyHttpError, match="forbidden"):
        http.get("https://api.alpaca.markets/v2/account")
    with pytest.raises(ReadOnlyHttpError, match="not allowed"):
        http.get("https://example.com/filings")
    state = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})
        return httpx.Response(200, json=FIXTURE["tickers"])

    adapter = adapter_for_tests(flaky)
    assert adapter._lookup_cik("AAPL") == "0000320193"
    assert state["n"] == 2
