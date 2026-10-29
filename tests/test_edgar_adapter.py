from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
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
    classify_period_kind,
    load_edgar_user_agent,
    ny_end_of_day,
    parse_sec_timestamp,
)
from atlas.ingest.http import ReadOnlyHttp, ReadOnlyHttpError
from atlas.ingest.snapshot import RevisionExistsError, SnapshotStore
from atlas.market_data.nyse import EASTERN

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
    eight_k = next(item for item in result.filings if item.accession.endswith("000081"))
    assert eight_k.time.event_time == eight_k.time.available_time
    assert eight_k.items == ("2.02", "9.01")
    assert eight_k.acceptance_verified is False
    assert eight_k.intraday_eligible is False
    assert eight_k.time.available_time == datetime(2024, 8, 1, 17, 31, 12, tzinfo=UTC)
    ten_k = next(item for item in result.filings if item.form_type == "10-K")
    assert ten_k.time.event_time.date().isoformat() == "2023-09-30"
    assert ten_k.time.available_time == datetime(2023, 11, 3, 18, 4, 28, tzinfo=UTC)
    assert ten_k.acceptance_verified is True
    assert ten_k.intraday_eligible is True
    assert ten_k.time.available_time > ten_k.time.event_time
    revenues = [fact for fact in result.facts if fact.concept == "Revenues"]
    annual = [item for item in revenues if item.period_kind == "duration_annual"]
    assert len(annual) == 2
    assert {item.accession for item in annual} == {
        "0000320193-23-000106",
        "0000320193-23-000077",
    }
    eps = [fact for fact in result.facts if fact.concept == "EarningsPerShareDiluted"]
    assert eps
    assert all(fact.unit == "USD/shares" for fact in eps)
    assert result.manifest.metadata["fact_coverage"]["unit_mismatch_rows"] >= 1
    assert result.statements
    annual_stmt = next(item for item in result.statements if item.period_kind == "duration_annual")
    assert annual_stmt.earnings_per_share == 6.13
    assert annual_stmt.book_value_per_share == pytest.approx(62146000000 / 15550061000)
    assert dict(annual_stmt.metrics)["revenue"] == 383285000000
    quarter = next(item for item in result.statements if item.period_kind == "duration_quarter")
    assert quarter.earnings_per_share == 1.53
    assert dict(quarter.metrics)["revenue"] == 90753000000
    assert dict(quarter.metrics)["revenue"] != 200000000000
    assert any(
        gap["reason"] == "incomplete_ratio_inputs"
        for gap in result.manifest.metadata["incomplete_statements"]
    )
    assert result.manifest.metadata["source_urls"]["companyfacts"].endswith(
        "/api/xbrl/companyfacts/CIK0000320193.json"
    )
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
    cik, payload = adapter._lookup_cik("AAPL")
    assert cik == "0000320193"
    assert payload["0"]["ticker"] == "AAPL"
    assert state["n"] == 2


def test_usd_shares_period_identity_and_conservative_timestamps(
    edgar_env: None, tmp_path: Path
) -> None:
    result = adapter_for_tests(_handler()).ingest(
        SnapshotStore(tmp_path),
        dataset_id="edgar-pit",
        revision="1",
        query=EdgarQuery(symbol="AAPL"),
    )
    naive = next(item for item in result.filings if item.accession.endswith("000200"))
    assert naive.acceptance_verified is False
    assert naive.intraday_eligible is False
    assert naive.raw_acceptance == "2024-03-11T09:30:00"
    assert naive.time.available_time == ny_end_of_day(date(2024, 3, 11))
    assert naive.time.available_time.astimezone(EASTERN).date() == date(2024, 3, 11)
    ytd = [
        fact
        for fact in result.facts
        if fact.concept == "Revenues" and fact.period_kind == "duration_ytd"
    ]
    assert ytd
    assert ytd[0].period_start is not None
    ids = {fact.ref_id for fact in result.facts}
    assert len(ids) == len(result.facts)
    coverage = result.manifest.metadata["fact_coverage"]
    assert "newest-first" in coverage["selection"]
    loaded = SnapshotStore(tmp_path).load("edgar-pit", "1")
    assert loaded.manifest.files["vendor/raw_bars.jsonl"]
    urls = {row["url"] for row in loaded.vendor_raw()}
    assert any("data.sec.gov" in url for url in urls)


def test_timestamp_conventions_and_max_facts_are_deterministic() -> None:
    zoned = parse_sec_timestamp("2024-08-01T16:31:12.000Z")
    assert zoned.verified is True
    assert zoned.intraday_eligible is True
    naive = parse_sec_timestamp("2024-03-11T09:30:00")
    assert naive.verified is False
    assert naive.available == ny_end_of_day(date(2024, 3, 11))
    dated = parse_sec_timestamp("2024-11-03")
    assert dated.intraday_eligible is False
    assert dated.available != datetime(2024, 11, 3, 23, 59, 59, tzinfo=UTC)
    assert dated.available == ny_end_of_day(date(2024, 11, 3))
    dst_spring = datetime(2024, 3, 11, 23, 59, 59, tzinfo=EASTERN)
    dst_fall = datetime(2024, 11, 3, 23, 59, 59, tzinfo=EASTERN)
    assert dst_spring.utcoffset() == timedelta(hours=-4)
    assert dst_fall.utcoffset() == timedelta(hours=-5)
    assert ny_end_of_day(date(2024, 3, 11)) == dst_spring.astimezone(UTC)
    assert classify_period_kind(date(2022, 9, 25), date(2023, 9, 30), "FY", "CY2023") == (
        "duration_annual"
    )
    assert classify_period_kind(date(2023, 12, 31), date(2024, 3, 30), "Q2", "CY2024Q1") == (
        "duration_quarter"
    )
    assert classify_period_kind(date(2023, 9, 24), date(2024, 3, 30), "Q2", "") == "duration_ytd"
    assert classify_period_kind(None, date(2023, 9, 30), "FY", "") == "instant"


def test_max_facts_truncates_newest_first(edgar_env: None, tmp_path: Path) -> None:
    result = adapter_for_tests(_handler()).ingest(
        SnapshotStore(tmp_path),
        dataset_id="edgar-cap",
        revision="1",
        query=EdgarQuery(symbol="AAPL", max_facts=3),
    )
    coverage = result.manifest.metadata["fact_coverage"]
    assert coverage["truncated"] is True
    assert coverage["stored_facts"] == 3
    assert coverage["omitted_facts"] > 0
    assert all(fact.unit in {"USD", "USD/shares", "shares"} for fact in result.facts)
