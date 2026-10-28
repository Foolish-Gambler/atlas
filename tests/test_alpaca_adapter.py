from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from atlas.ingest.alpaca import (
    AlpacaBarQuery,
    AlpacaIntegrityError,
    MissingCredentialsError,
    adapter_for_tests,
    load_alpaca_credentials,
)
from atlas.ingest.http import NetworkDisabledError, ReadOnlyHttp, ReadOnlyHttpError
from atlas.ingest.snapshot import RevisionExistsError, SnapshotStore
from atlas.market_data.nyse import EASTERN
from atlas.paper_trading.demo import ExecutionUnsuitableError, run_virtual_portfolio
from atlas.validation import InvalidInputError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ingest" / "alpaca"
MIN_PAGES = json.loads((FIXTURE_DIR / "aapl_1min_pages.json").read_text(encoding="utf-8"))["pages"]
DAY_PAGES = json.loads((FIXTURE_DIR / "aapl_1day_pages.json").read_text(encoding="utf-8"))["pages"]


@pytest.fixture
def alpaca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def _handler(pages: list[dict], log: list[httpx.Request] | None = None):
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        if request.method != "GET":
            return httpx.Response(405, json={"error": "GET only"})
        page = pages[state["i"] % len(pages)]
        state["i"] += 1
        return httpx.Response(200, json=page)

    return handler


def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(MissingCredentialsError, match="APCA_API_KEY_ID"):
        load_alpaca_credentials()


def test_network_disabled_by_default(alpaca_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_ALLOW_NETWORK", raising=False)
    with pytest.raises(NetworkDisabledError, match="disabled"):
        ReadOnlyHttp(allowed_host="data.alpaca.markets")


def test_pagination_and_get_only(alpaca_env: None, tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    adapter = adapter_for_tests(_handler(MIN_PAGES, log))
    store = SnapshotStore(tmp_path)
    query = AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 3), timeframe="1Min")
    result = adapter.ingest(store, dataset_id="alpaca-aapl", revision="mock-min", query=query)
    assert len(log) == 2
    assert all(request.method == "GET" for request in log)
    assert all(request.url.host == "data.alpaca.markets" for request in log)
    assert log[0].url.params.get("page_token") in {None, ""}
    assert log[0].url.params["feed"] == "iex"
    assert log[0].url.params["adjustment"] == "raw"
    assert log[1].url.params["page_token"] == "page-2"
    assert "test-secret" not in str(result.manifest.to_dict())
    assert result.execution_eligible is True
    assert result.simulated is True
    assert result.authenticated_vendor_data is False
    assert result.retrieval_mode == "mocked_http"
    assert result.manifest.metadata["authenticated_vendor_data"] is False
    assert "Mocked HTTP fixture" in result.manifest.metadata["price_disclaimer"]
    assert result.manifest.vendor == "alpaca"
    assert result.manifest.source_kind.value == "simulated"
    bars = result.bars
    assert len(bars) == 2
    first = bars[0]
    assert first.session_open_verified is True
    assert first.open_time.astimezone(EASTERN).hour == 9
    assert first.open_time.astimezone(EASTERN).minute == 30
    assert first.close == 101.2
    assert first.open == 100.0
    assert first.vendor_time is not None
    assert first.requires_open_interval_evidence is True
    assert "open_is_one_minute_bucket" in first.quality_flags
    loaded = store.load("alpaca-aapl", "mock-min")
    assert loaded.manifest.metadata["retrieval_mode"] == "mocked_http"
    assert loaded.all_bars()[0].session_open_verified is True
    assert loaded.all_bars()[0].quality_flags == first.quality_flags
    with pytest.raises(RevisionExistsError):
        adapter.ingest(store, dataset_id="alpaca-aapl", revision="mock-min", query=query)


def test_daily_bars_are_not_execution_eligible(alpaca_env: None, tmp_path: Path) -> None:
    adapter = adapter_for_tests(_handler(DAY_PAGES))
    store = SnapshotStore(tmp_path)
    query = AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Day")
    result = adapter.ingest(store, dataset_id="alpaca-aapl-day", revision="mock-day", query=query)
    assert result.execution_eligible is False
    bar = result.bars[0]
    assert bar.session_open_verified is False
    vendor_et = bar.vendor_time.astimezone(EASTERN)
    assert vendor_et.hour == 0
    assert bar.open_time.hour == 9 and bar.open_time.minute == 30
    assert bar.vendor_time != bar.open_time
    source = store.load("alpaca-aapl-day", "mock-day")
    with pytest.raises(ExecutionUnsuitableError, match="unsuitable"):
        run_virtual_portfolio(source)


def test_http_errors_and_retry(alpaca_env: None, tmp_path: Path) -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate"})
        return httpx.Response(200, json=MIN_PAGES[0] | {"next_page_token": None})

    adapter = adapter_for_tests(handler)
    query = AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min")
    result = adapter.ingest(SnapshotStore(tmp_path), dataset_id="a", revision="r1", query=query)
    assert state["n"] == 2
    assert result.bars[0].symbol == "AAPL"

    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad"})

    with pytest.raises(ReadOnlyHttpError, match="400"):
        adapter_for_tests(fail).fetch_bars(query)


def test_dst_and_early_close_minute_aggregation(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {"t": "2024-03-08T14:30:00Z", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1},
                {"t": "2024-03-08T20:59:00Z", "o": 10.6, "h": 10.7, "l": 10.4, "c": 10.6, "v": 1},
                {"t": "2024-03-11T13:30:00Z", "o": 12.0, "h": 12.2, "l": 11.8, "c": 12.1, "v": 1},
                {"t": "2024-03-11T19:59:00Z", "o": 12.2, "h": 12.3, "l": 12.0, "c": 12.2, "v": 1},
                {"t": "2024-11-29T14:30:00Z", "o": 13.0, "h": 13.1, "l": 12.9, "c": 13.0, "v": 1},
                {"t": "2024-11-29T17:59:00Z", "o": 13.2, "h": 13.3, "l": 13.1, "c": 13.2, "v": 1},
                {"t": "2024-11-29T20:59:00Z", "o": 99.0, "h": 99.0, "l": 99.0, "c": 99.0, "v": 1},
            ],
            "next_page_token": None,
        }
    ]
    adapter = adapter_for_tests(_handler(pages))
    query = AlpacaBarQuery(
        start=date(2024, 3, 8),
        end=date(2024, 11, 29),
        timeframe="1Min",
        max_query_days=400,
    )
    result = adapter.ingest(SnapshotStore(tmp_path), dataset_id="cal", revision="1", query=query)
    by_close = {bar.close_time.astimezone(EASTERN).date(): bar for bar in result.bars}
    friday = by_close[date(2024, 3, 8)]
    monday = by_close[date(2024, 3, 11)]
    early = by_close[date(2024, 11, 29)]
    assert friday.open_time.utcoffset().total_seconds() == -5 * 3600
    assert monday.open_time.utcoffset().total_seconds() == -4 * 3600
    assert early.close_time.astimezone(EASTERN).hour == 13
    assert early.close == 13.2
    assert early.high < 99.0
    assert "2024-03-12" in result.missing_sessions


def test_malformed_and_non_finite_bars(alpaca_env: None, tmp_path: Path) -> None:
    bad = [{"bars": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 1, "l": 2, "c": 1, "v": 1}]}]
    with pytest.raises(AlpacaIntegrityError, match="OHLCV"):
        adapter_for_tests(_handler(bad)).ingest(
            SnapshotStore(tmp_path),
            dataset_id="bad-ohlc",
            revision="1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
        )

    def inf_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"bars":[{"t":"2024-01-02T14:30:00Z","o":1,"h":1,"l":1,"c":null,"v":1}]}',
        )

    with pytest.raises(AlpacaIntegrityError):
        adapter_for_tests(inf_handler).ingest(
            SnapshotStore(tmp_path),
            dataset_id="bad-null",
            revision="1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
        )


def test_duplicate_daily_rejected(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {"t": "2024-01-02T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                {"t": "2024-01-02T05:00:00-05:00", "o": 2, "h": 2, "l": 2, "c": 2, "v": 1},
            ],
            "next_page_token": None,
        }
    ]
    with pytest.raises(AlpacaIntegrityError, match="duplicate"):
        adapter_for_tests(_handler(pages)).ingest(
            SnapshotStore(tmp_path),
            dataset_id="dup",
            revision="1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Day"),
        )


def test_late_first_minute_is_not_a_verified_open(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {"t": "2024-01-02T14:45:00Z", "o": 99.0, "h": 99.5, "l": 98.5, "c": 99.2, "v": 10},
                {
                    "t": "2024-01-02T20:59:00Z",
                    "o": 100.0,
                    "h": 100.1,
                    "l": 99.9,
                    "c": 100.0,
                    "v": 10,
                },
                {
                    "t": "2024-01-03T14:30:00Z",
                    "o": 102.0,
                    "h": 102.2,
                    "l": 101.8,
                    "c": 102.0,
                    "v": 10,
                },
                {
                    "t": "2024-01-03T20:59:00Z",
                    "o": 103.0,
                    "h": 103.1,
                    "l": 102.9,
                    "c": 103.0,
                    "v": 10,
                },
            ],
            "next_page_token": None,
        }
    ]
    result = adapter_for_tests(_handler(pages)).ingest(
        SnapshotStore(tmp_path),
        dataset_id="late-open",
        revision="1",
        query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 3), timeframe="1Min"),
    )
    first, second = result.bars
    assert first.session_open_verified is False
    assert "open_interval_missing" in first.quality_flags
    assert first.observed_open_time is not None
    assert first.observed_open_time.hour == 9 and first.observed_open_time.minute == 45
    assert first.expected_open_time is not None
    assert first.expected_open_time.minute == 30
    assert second.session_open_verified is True
    demo = run_virtual_portfolio(SnapshotStore(tmp_path).load("late-open", "1"), run_id="late")
    fill = next(row for row in demo.result.journal.records if row["type"] == "fill")
    assert fill["fill_price"] == 102.0
    assert "09:45" not in fill["execution_time"]


def test_zero_volume_open_interval_is_unverified(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {
                    "t": "2024-01-02T14:30:00Z",
                    "o": 100.0,
                    "h": 100.0,
                    "l": 100.0,
                    "c": 100.0,
                    "v": 0,
                },
                {
                    "t": "2024-01-02T20:59:00Z",
                    "o": 101.0,
                    "h": 101.0,
                    "l": 101.0,
                    "c": 101.0,
                    "v": 5,
                },
            ],
            "next_page_token": None,
        }
    ]
    result = adapter_for_tests(_handler(pages)).ingest(
        SnapshotStore(tmp_path),
        dataset_id="zero-open",
        revision="1",
        query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
    )
    bar = result.bars[0]
    assert bar.session_open_verified is False
    assert "open_interval_zero_volume" in bar.quality_flags
    assert result.execution_eligible is False
    quality = result.manifest.metadata["session_quality"][0]
    assert quality["open_interval_observed"] is True
    assert quality["open_volume"] == 0
    assert quality["execution_eligible"] is False


def test_unsupported_calendar_year_is_rejected() -> None:
    from atlas.market_data.nyse import UnsupportedCalendarError

    with pytest.raises(UnsupportedCalendarError, match="2024"):
        AlpacaBarQuery(start=date(2023, 1, 3), end=date(2023, 1, 4), timeframe="1Min")


def test_query_bounds_and_repeated_page_token(alpaca_env: None, tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="max_query_days"):
        AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 3, 1), timeframe="1Min")

    looping = [
        {
            "bars": [
                {
                    "t": "2024-01-02T14:30:00Z",
                    "o": 100.0,
                    "h": 100.0,
                    "l": 100.0,
                    "c": 100.0,
                    "v": 1,
                }
            ],
            "next_page_token": "same-token",
        }
    ]
    store = SnapshotStore(tmp_path)
    with pytest.raises(AlpacaIntegrityError, match="repeated pagination token"):
        adapter_for_tests(_handler(looping)).ingest(
            store,
            dataset_id="loop",
            revision="1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
        )
    assert not (tmp_path / "loop" / "1").exists()

    oversized = [
        {
            "bars": [
                {
                    "t": "2024-01-02T14:30:00Z",
                    "o": 1.0,
                    "h": 1.0,
                    "l": 1.0,
                    "c": 1.0,
                    "v": 1,
                }
            ]
            * 3,
            "next_page_token": None,
        }
    ]
    with pytest.raises(AlpacaIntegrityError, match="max_records"):
        adapter_for_tests(_handler(oversized)).fetch_bars(
            AlpacaBarQuery(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                timeframe="1Min",
                max_records=2,
            )
        )


def test_max_pages_is_enforced(alpaca_env: None) -> None:
    pages = [
        {
            "bars": [
                {
                    "t": f"2024-01-02T14:{index:02d}:00Z",
                    "o": 1.0,
                    "h": 1.0,
                    "l": 1.0,
                    "c": 1.0,
                    "v": 1,
                }
            ],
            "next_page_token": f"t-{index}",
        }
        for index in range(5)
    ]

    with pytest.raises(AlpacaIntegrityError, match="max_pages"):
        adapter_for_tests(_handler(pages)).fetch_bars(
            AlpacaBarQuery(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                timeframe="1Min",
                max_pages=2,
            )
        )


def test_failed_ingest_does_not_publish_revision(alpaca_env: None, tmp_path: Path) -> None:
    bad = [{"bars": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 1, "l": 2, "c": 1, "v": 1}]}]
    store = SnapshotStore(tmp_path)
    with pytest.raises(AlpacaIntegrityError):
        adapter_for_tests(_handler(bad)).ingest(
            store,
            dataset_id="partial",
            revision="1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
        )
    assert not (tmp_path / "partial" / "1").exists()


def test_minute_gaps_are_not_vendor_failures(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {
                    "t": "2024-01-02T14:30:00Z",
                    "o": 100.0,
                    "h": 100.1,
                    "l": 99.9,
                    "c": 100.0,
                    "v": 10,
                },
                {
                    "t": "2024-01-02T15:00:00Z",
                    "o": 100.2,
                    "h": 100.2,
                    "l": 100.2,
                    "c": 100.2,
                    "v": 0,
                },
                {
                    "t": "2024-01-02T20:00:00Z",
                    "o": 101.0,
                    "h": 101.0,
                    "l": 101.0,
                    "c": 101.0,
                    "v": 5,
                },
            ],
            "next_page_token": None,
        }
    ]
    result = adapter_for_tests(_handler(pages)).ingest(
        SnapshotStore(tmp_path),
        dataset_id="gaps",
        revision="1",
        query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 2), timeframe="1Min"),
    )
    bar = result.bars[0]
    quality = result.manifest.metadata["session_quality"][0]
    assert bar.session_open_verified is True
    assert bar.session_close_verified is False
    assert "incomplete_close_coverage" in bar.quality_flags
    assert "observed_minute_gaps" in bar.quality_flags
    assert "no_trade_intervals" in bar.quality_flags
    assert "vendor_failure" not in bar.quality_flags
    assert quality["execution_eligible"] is True
    assert quality["full_session_validated"] is False
    assert quality["gap_is_not_vendor_failure"] is True
    assert quality["no_trade_interval_count"] == 1
    assert quality["observed_gap_count"] > 0
    assert bar.close_time.astimezone(EASTERN).hour == 15
    assert bar.expected_close_time is not None
    assert bar.expected_close_time.astimezone(EASTERN).hour == 16
    loaded = SnapshotStore(tmp_path).load("gaps", "1")
    assert loaded.all_bars()[0].session_close_verified is False
    assert loaded.manifest.metadata["retrieval_mode"] == "mocked_http"
    assert loaded.all_bars()[0].vendor_time is not None
    assert result.manifest.metadata["full_session_validated"] is False
    assert result.manifest.metadata["validated_session_count"] == 0
    assert result.manifest.metadata["incomplete_session_count"] == 1
    assert result.execution_eligible is True
    assert loaded.vendor_raw()
    assert "vendor/raw_bars.jsonl" in loaded.manifest.files


def test_mixed_session_quality_does_not_mark_dataset_complete(
    alpaca_env: None, tmp_path: Path
) -> None:
    def minutes(day: date, *, skip_last: bool) -> list[dict]:
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN).astimezone(UTC)
        count = 390
        rows = []
        for index in range(count):
            if skip_last and index == count - 1:
                continue
            stamp = start + timedelta(minutes=index)
            rows.append(
                {
                    "t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": 100.0,
                    "h": 100.2,
                    "l": 99.8,
                    "c": 100.1,
                    "v": 4,
                }
            )
        return rows

    pages = [
        {
            "bars": minutes(date(2024, 1, 2), skip_last=False)
            + minutes(date(2024, 1, 3), skip_last=True),
            "next_page_token": None,
        }
    ]
    store = SnapshotStore(tmp_path)
    result = adapter_for_tests(_handler(pages)).ingest(
        store,
        dataset_id="mixed-quality",
        revision="agg-1",
        query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 3), timeframe="1Min"),
    )
    qualities = result.manifest.metadata["session_quality"]
    assert len(qualities) == 2
    assert qualities[0]["full_session_validated"] is True
    assert qualities[1]["full_session_validated"] is False
    assert result.manifest.metadata["full_session_validated"] is False
    assert result.manifest.metadata["validated_session_count"] == 1
    assert result.manifest.metadata["incomplete_session_count"] == 1
    assert result.manifest.metadata["included_session_count"] == 2
    assert result.manifest.metadata["missing_session_count"] == 0
    assert result.execution_eligible is True
    assert result.manifest.metadata["execution_eligible"] is True
    loaded = store.load("mixed-quality", "agg-1")
    assert loaded.manifest.counts["vendor_raw"] == 779
    assert len(loaded.vendor_raw()) == 779
    digest = loaded.manifest.files["vendor/raw_bars.jsonl"]
    assert len(digest) == 64
    with pytest.raises(RevisionExistsError):
        adapter_for_tests(_handler(pages)).ingest(
            store,
            dataset_id="mixed-quality",
            revision="agg-1",
            query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 3), timeframe="1Min"),
        )


def test_january_9_2025_is_not_an_expected_session(alpaca_env: None, tmp_path: Path) -> None:
    pages = [
        {
            "bars": [
                {
                    "t": "2025-01-08T14:30:00Z",
                    "o": 10.0,
                    "h": 10.0,
                    "l": 10.0,
                    "c": 10.0,
                    "v": 1,
                },
                {
                    "t": "2025-01-08T20:59:00Z",
                    "o": 10.1,
                    "h": 10.1,
                    "l": 10.1,
                    "c": 10.1,
                    "v": 1,
                },
                {
                    "t": "2025-01-09T14:30:00Z",
                    "o": 99.0,
                    "h": 99.0,
                    "l": 99.0,
                    "c": 99.0,
                    "v": 1,
                },
                {
                    "t": "2025-01-10T14:30:00Z",
                    "o": 11.0,
                    "h": 11.0,
                    "l": 11.0,
                    "c": 11.0,
                    "v": 1,
                },
                {
                    "t": "2025-01-10T20:59:00Z",
                    "o": 11.1,
                    "h": 11.1,
                    "l": 11.1,
                    "c": 11.1,
                    "v": 1,
                },
            ],
            "next_page_token": None,
        }
    ]
    result = adapter_for_tests(_handler(pages)).ingest(
        SnapshotStore(tmp_path),
        dataset_id="carter",
        revision="1",
        query=AlpacaBarQuery(start=date(2025, 1, 8), end=date(2025, 1, 10), timeframe="1Min"),
    )
    dates = {bar.close_time.astimezone(EASTERN).date() for bar in result.bars}
    assert date(2025, 1, 9) not in dates
    assert "2025-01-09" not in result.missing_sessions
    assert {bar.open_time.astimezone(EASTERN).date() for bar in result.bars} == {
        date(2025, 1, 8),
        date(2025, 1, 10),
    }


def test_trading_endpoints_are_forbidden(alpaca_env: None) -> None:
    http = ReadOnlyHttp(
        allowed_host="data.alpaca.markets",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        allow_network=False,
        rate_limit_sleep=0.0,
        sleeper=lambda _delay: None,
    )
    with pytest.raises(ReadOnlyHttpError, match="forbidden"):
        http.get("https://api.alpaca.markets/v2/orders")
