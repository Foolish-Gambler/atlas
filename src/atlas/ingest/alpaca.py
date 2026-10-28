"""Read-only Alpaca historical bars. No order routing, no live brokerage."""

from __future__ import annotations

import math
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from atlas.contracts import MarketBarRecord, Provenance, SourceKind
from atlas.ingest.http import ALPACA_DATA_HOST, ReadOnlyHttp
from atlas.ingest.snapshot import SnapshotManifest, SnapshotPayload, SnapshotStore
from atlas.market_data.nyse import (
    EASTERN,
    SUPPORTED_YEARS,
    NyseCalendar,
    NyseSession,
    UnsupportedCalendarError,
)
from atlas.validation import InvalidInputError, normalize_symbol, require_finite

ADAPTER_VERSION = "atlas.alpaca.bars.v1"
RETRIEVAL_MOCKED_HTTP = "mocked_http"
RETRIEVAL_VENDOR_GET = "vendor_get"
DEFAULT_SYMBOL = "AAPL"
DEFAULT_START = date(2024, 1, 2)
DEFAULT_END = date(2024, 1, 2)
DEFAULT_FEED = "iex"
DEFAULT_ADJUSTMENT = "raw"
DEFAULT_MAX_QUERY_DAYS = 31
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_RECORDS = 50_000
_GAP_SAMPLE_LIMIT = 32
_ALLOWED_TIMEFRAMES = frozenset({"1Day", "1Min"})
_BARS_PATH = "/v2/stocks/{symbol}/bars"


class MissingCredentialsError(InvalidInputError):
    """Alpaca data credentials are absent from the environment."""


class AlpacaIntegrityError(InvalidInputError):
    """Vendor bars failed calendar, duplicate, or OHLCV checks."""


@dataclass(frozen=True, slots=True)
class AlpacaBarQuery:
    symbol: str = DEFAULT_SYMBOL
    start: date = DEFAULT_START
    end: date = DEFAULT_END
    feed: str = DEFAULT_FEED
    adjustment: str = DEFAULT_ADJUSTMENT
    timeframe: str = "1Min"
    currency: str = "USD"
    limit: int = 10_000
    max_query_days: int = DEFAULT_MAX_QUERY_DAYS
    max_pages: int = DEFAULT_MAX_PAGES
    max_records: int = DEFAULT_MAX_RECORDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if self.timeframe not in _ALLOWED_TIMEFRAMES:
            raise InvalidInputError("timeframe must be 1Day or 1Min.")
        if self.feed != DEFAULT_FEED:
            raise InvalidInputError("M2 alpaca ingest supports feed=iex only.")
        if self.adjustment != DEFAULT_ADJUSTMENT:
            raise InvalidInputError("M2 alpaca ingest supports adjustment=raw only.")
        if self.end < self.start:
            raise InvalidInputError("end cannot precede start.")
        if self.max_query_days < 1 or self.max_pages < 1 or self.max_records < 1:
            raise InvalidInputError("query bounds must be positive.")
        span_days = (self.end - self.start).days + 1
        if span_days > self.max_query_days:
            raise InvalidInputError(
                f"query span {span_days} days exceeds max_query_days={self.max_query_days}."
            )
        if self.start.year not in SUPPORTED_YEARS or self.end.year not in SUPPORTED_YEARS:
            raise UnsupportedCalendarError(
                "Alpaca ingest query must stay within the maintained NYSE calendar years "
                f"{sorted(SUPPORTED_YEARS)}."
            )


@dataclass(frozen=True, slots=True)
class _RawBar:
    vendor_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True, slots=True)
class AlpacaIngestResult:
    manifest: SnapshotManifest
    bars: tuple[MarketBarRecord, ...]
    missing_sessions: tuple[str, ...]
    execution_eligible: bool
    retrieval_mode: str
    authenticated_vendor_data: bool
    simulated: bool


def load_alpaca_credentials() -> tuple[str, str]:
    key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise MissingCredentialsError(
            "APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set in the environment."
        )
    return key, secret


class AlpacaBarsAdapter:
    """GET historical OHLCV from data.alpaca.markets and store an immutable snapshot."""

    def __init__(
        self,
        *,
        http: ReadOnlyHttp | None = None,
        calendar: NyseCalendar | None = None,
        allow_network: bool | None = None,
    ) -> None:
        self._key, self._secret = load_alpaca_credentials()
        self.calendar = calendar or NyseCalendar()
        self.http = http or ReadOnlyHttp(
            allowed_host=ALPACA_DATA_HOST,
            allow_network=allow_network,
            rate_limit_sleep=0.2,
        )

    def fetch_bars(self, query: AlpacaBarQuery | None = None) -> list[dict[str, Any]]:
        query = query or AlpacaBarQuery()
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        pages = 0
        path = _BARS_PATH.format(symbol=query.symbol)
        url = f"https://{ALPACA_DATA_HOST}{path}"
        while True:
            pages += 1
            if pages > query.max_pages:
                raise AlpacaIntegrityError(f"pagination exceeded max_pages={query.max_pages}.")
            params = {
                "timeframe": query.timeframe,
                "start": f"{query.start.isoformat()}T00:00:00Z",
                "end": f"{query.end.isoformat()}T23:59:59Z",
                "adjustment": query.adjustment,
                "feed": query.feed,
                "limit": str(query.limit),
                "currency": query.currency,
            }
            if page_token:
                params["page_token"] = page_token
            response = self.http.get(url, params=params, headers=self._auth_headers())
            payload = response.json()
            if not isinstance(payload, dict):
                raise AlpacaIntegrityError("Alpaca bars response must be a JSON object.")
            bars = payload.get("bars") or []
            if not isinstance(bars, list):
                raise AlpacaIntegrityError("Alpaca bars must be a list.")
            rows.extend(bars)
            if len(rows) > query.max_records:
                raise AlpacaIntegrityError(f"response exceeded max_records={query.max_records}.")
            token = payload.get("next_page_token")
            if not token:
                break
            token_text = str(token)
            if token_text in seen_tokens or token_text == page_token:
                raise AlpacaIntegrityError("repeated pagination token; refusing to loop.")
            seen_tokens.add(token_text)
            page_token = token_text
        return rows

    def ingest(
        self,
        store: SnapshotStore,
        *,
        dataset_id: str,
        revision: str,
        query: AlpacaBarQuery | None = None,
        created_at: datetime | None = None,
        retrieval_mode: str = RETRIEVAL_MOCKED_HTTP,
    ) -> AlpacaIngestResult:
        query = query or AlpacaBarQuery()
        if retrieval_mode not in {RETRIEVAL_MOCKED_HTTP, RETRIEVAL_VENDOR_GET}:
            raise InvalidInputError("retrieval_mode must be mocked_http or vendor_get.")
        mocked = retrieval_mode == RETRIEVAL_MOCKED_HTTP
        ingested_at = created_at or datetime.now(UTC)
        raw_rows = self.fetch_bars(query)
        raw = [_parse_vendor_bar(row) for row in raw_rows]
        provenance = Provenance(
            source_kind=SourceKind.SIMULATED if mocked else SourceKind.VENDOR_READ_ONLY,
            dataset_id=dataset_id,
            revision=revision,
            vendor="alpaca",
        )
        if query.timeframe == "1Min":
            bars, missing, qualities = _from_minutes(
                raw, query, provenance, ingested_at, self.calendar
            )
        else:
            bars, missing, qualities = _from_daily(
                raw, query, provenance, ingested_at, self.calendar
            )
        eligible = any(item["execution_eligible"] for item in qualities)
        metadata = _ingest_metadata(
            query,
            missing=missing,
            qualities=qualities,
            eligible=eligible,
            mocked=mocked,
            ingested_at=ingested_at,
        )
        dest = store.revision_path(dataset_id, revision)
        try:
            manifest = store.create(
                dataset_id,
                revision,
                SnapshotPayload(bars=tuple(bars)),
                source_kind=provenance.source_kind,
                vendor="alpaca",
                created_at=ingested_at,
                metadata=metadata,
            )
        except Exception:
            if dest.exists() and not (dest / "manifest.json").is_file():
                shutil.rmtree(dest, ignore_errors=True)
            raise
        return AlpacaIngestResult(
            manifest=manifest,
            bars=tuple(bars),
            missing_sessions=missing,
            execution_eligible=eligible,
            retrieval_mode=retrieval_mode,
            authenticated_vendor_data=not mocked,
            simulated=mocked,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
        }


def mock_alpaca_client(handler: Any) -> httpx.Client:
    """httpx client that never opens a real socket."""

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=f"https://{ALPACA_DATA_HOST}",
        follow_redirects=False,
    )


def adapter_for_tests(handler: Any) -> AlpacaBarsAdapter:
    http = ReadOnlyHttp(
        allowed_host=ALPACA_DATA_HOST,
        client=mock_alpaca_client(handler),
        allow_network=False,
        rate_limit_sleep=0.0,
        sleeper=lambda _delay: None,
    )
    return AlpacaBarsAdapter(http=http)


def _parse_vendor_bar(row: dict[str, Any]) -> _RawBar:
    try:
        vendor_time = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise AlpacaIntegrityError("bar timestamp t is required and must be ISO-8601.") from exc
    if vendor_time.tzinfo is None:
        raise AlpacaIntegrityError("vendor timestamp must be timezone-aware.")
    try:
        open_ = require_finite(row["o"], "open")
        high = require_finite(row["h"], "high")
        low = require_finite(row["l"], "low")
        close = require_finite(row["c"], "close")
        volume = int(row["v"])
    except (KeyError, TypeError, ValueError, InvalidInputError) as exc:
        raise AlpacaIntegrityError("bar is missing OHLCV fields.") from exc
    for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if not math.isfinite(value) or value <= 0:
            raise AlpacaIntegrityError(f"non-finite or non-positive {name}.")
    if volume < 0:
        raise AlpacaIntegrityError("volume must be non-negative.")
    if high < low or high < open_ or high < close or low > open_ or low > close:
        raise AlpacaIntegrityError("malformed OHLCV relationship.")
    return _RawBar(vendor_time, open_, high, low, close, volume)


def _from_daily(
    raw: Sequence[_RawBar],
    query: AlpacaBarQuery,
    provenance: Provenance,
    ingested_at: datetime,
    calendar: NyseCalendar,
) -> tuple[list[MarketBarRecord], tuple[str, ...], list[dict[str, Any]]]:
    by_session: dict[date, _RawBar] = {}
    for bar in raw:
        labeled = bar.vendor_time.astimezone(EASTERN).date()
        session = calendar.session_on(labeled)
        if session is None:
            continue
        if session.session_date in by_session:
            raise AlpacaIntegrityError(
                f"duplicate daily bar for {session.session_date.isoformat()}"
            )
        by_session[session.session_date] = bar
    expected = {session.session_date for session in calendar.sessions(query.start, query.end)}
    missing = tuple(sorted(day.isoformat() for day in expected - set(by_session)))
    records = []
    qualities: list[dict[str, Any]] = []
    for day, bar in sorted(by_session.items()):
        session = calendar.session_on(day)
        assert session is not None
        flags = (
            "daily_aggregate_unverified_open",
            "daily_aggregate_unverified_close",
            "open_is_not_rth_print",
            "execution_unsuitable",
        )
        records.append(
            _session_bar(
                query.symbol,
                session,
                provenance,
                ingested_at,
                open_px=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                vendor_time=bar.vendor_time,
                open_verified=False,
                close_verified=False,
                close_time=session.close,
                observed_open=None,
                observed_last=None,
                flags=flags,
            )
        )
        qualities.append(
            _quality_row(
                session,
                observed_open=None,
                observed_last=None,
                open_interval_observed=False,
                close_interval_observed=False,
                open_volume=None,
                open_verified=False,
                close_verified=False,
                flags=flags,
            )
        )
    return records, missing, qualities


def _from_minutes(
    raw: Sequence[_RawBar],
    query: AlpacaBarQuery,
    provenance: Provenance,
    ingested_at: datetime,
    calendar: NyseCalendar,
) -> tuple[list[MarketBarRecord], tuple[str, ...], list[dict[str, Any]]]:
    grouped: dict[date, list[_RawBar]] = {}
    for bar in raw:
        local = bar.vendor_time.astimezone(EASTERN)
        session = calendar.session_on(local.date())
        if session is None or not session.contains(local):
            continue
        grouped.setdefault(session.session_date, []).append(bar)
    expected = {session.session_date for session in calendar.sessions(query.start, query.end)}
    missing = tuple(sorted(day.isoformat() for day in expected - set(grouped)))
    records = []
    qualities: list[dict[str, Any]] = []
    for day, rows in sorted(grouped.items()):
        session = calendar.session_on(day)
        assert session is not None
        ordered = sorted(rows, key=lambda item: item.vendor_time)
        times = [item.vendor_time for item in ordered]
        if len(times) != len(set(times)):
            raise AlpacaIntegrityError(f"duplicate minute bars for {day.isoformat()}")
        first, last = ordered[0], ordered[-1]
        by_minute = {item.vendor_time.astimezone(EASTERN): item for item in ordered}
        expected_minutes = _expected_minutes(session)
        opening = by_minute.get(session.open)
        last_expected = session.close - timedelta(minutes=1)
        closing = by_minute.get(last_expected)
        gaps = [stamp for stamp in expected_minutes if stamp not in by_minute]
        no_trades = [
            stamp.isoformat() for stamp, item in sorted(by_minute.items()) if item.volume == 0
        ]
        flags: list[str] = ["open_is_one_minute_bucket"]
        open_interval_observed = opening is not None
        open_volume = opening.volume if opening is not None else None
        if opening is None:
            flags.append("open_interval_missing")
        elif open_volume == 0:
            flags.append("open_interval_zero_volume")
        close_verified = closing is not None
        if not close_verified:
            flags.append("incomplete_close_coverage")
        if gaps:
            flags.append("observed_minute_gaps")
        if no_trades:
            flags.append("no_trade_intervals")
        open_verified = opening is not None and (open_volume or 0) > 0
        if not open_verified:
            flags.append("execution_unsuitable")
        full_session = open_interval_observed and close_verified and not gaps
        if not full_session:
            flags.append("session_incomplete")
        open_px = opening.open if open_verified else first.open
        observed_last = last.vendor_time.astimezone(EASTERN)
        close_time = session.close if close_verified else observed_last + timedelta(minutes=1)
        records.append(
            _session_bar(
                query.symbol,
                session,
                provenance,
                ingested_at,
                open_px=open_px,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=last.close,
                volume=sum(item.volume for item in ordered),
                vendor_time=first.vendor_time,
                open_verified=open_verified,
                close_verified=close_verified,
                close_time=close_time,
                observed_open=first.vendor_time.astimezone(EASTERN),
                observed_last=observed_last,
                flags=tuple(flags),
            )
        )
        qualities.append(
            _quality_row(
                session,
                observed_open=first.vendor_time.astimezone(EASTERN),
                observed_last=observed_last,
                open_interval_observed=open_interval_observed,
                close_interval_observed=close_verified,
                open_volume=open_volume,
                open_verified=open_verified,
                close_verified=close_verified,
                flags=tuple(flags),
                expected_minute_count=len(expected_minutes),
                observed_minute_count=len(by_minute),
                no_trade_interval_count=len(no_trades),
                observed_gap_count=len(gaps),
                observed_gaps=[stamp.isoformat() for stamp in gaps[:_GAP_SAMPLE_LIMIT]],
                no_trade_intervals=no_trades[:_GAP_SAMPLE_LIMIT],
            )
        )
    return records, missing, qualities


def _session_bar(
    symbol: str,
    session: NyseSession,
    provenance: Provenance,
    ingested_at: datetime,
    *,
    open_px: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    vendor_time: datetime,
    open_verified: bool,
    close_verified: bool,
    close_time: datetime,
    observed_open: datetime | None,
    observed_last: datetime | None,
    flags: tuple[str, ...],
) -> MarketBarRecord:
    return MarketBarRecord(
        symbol=symbol,
        open_time=session.open if open_verified else (observed_open or session.open),
        close_time=close_time,
        available_time=close_time,
        provenance=provenance,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ingested_time=ingested_at,
        vendor_time=vendor_time,
        session_open_verified=open_verified,
        session_close_verified=close_verified,
        requires_open_interval_evidence=True,
        expected_open_time=session.open,
        expected_close_time=session.close,
        observed_open_time=observed_open,
        observed_last_time=observed_last,
        quality_flags=flags,
    )


def _quality_row(
    session: NyseSession,
    *,
    observed_open: datetime | None,
    observed_last: datetime | None,
    open_interval_observed: bool,
    close_interval_observed: bool,
    open_volume: int | None,
    open_verified: bool,
    close_verified: bool,
    flags: tuple[str, ...],
    expected_minute_count: int | None = None,
    observed_minute_count: int | None = None,
    no_trade_interval_count: int = 0,
    observed_gap_count: int = 0,
    observed_gaps: list[str] | None = None,
    no_trade_intervals: list[str] | None = None,
) -> dict[str, Any]:
    full_session = bool(
        open_interval_observed
        and close_interval_observed
        and observed_gap_count == 0
        and (expected_minute_count is None or observed_minute_count == expected_minute_count)
    )
    return {
        "session_date": session.session_date.isoformat(),
        "expected_open": session.open.isoformat(),
        "expected_close": session.close.isoformat(),
        "observed_open": observed_open.isoformat() if observed_open else None,
        "observed_last": observed_last.isoformat() if observed_last else None,
        "open_interval_observed": open_interval_observed,
        "close_interval_observed": close_interval_observed,
        "open_volume": open_volume,
        "session_open_verified": open_verified,
        "session_close_verified": close_verified,
        "execution_eligible": open_verified,
        "full_session_validated": full_session,
        "expected_minute_count": expected_minute_count,
        "observed_minute_count": observed_minute_count,
        "no_trade_interval_count": no_trade_interval_count,
        "observed_gap_count": observed_gap_count,
        "observed_gaps": list(observed_gaps or ()),
        "no_trade_intervals": list(no_trade_intervals or ()),
        "gap_is_not_vendor_failure": True,
        "flags": list(flags),
    }


def _ingest_metadata(
    query: AlpacaBarQuery,
    *,
    missing: tuple[str, ...],
    qualities: list[dict[str, Any]],
    eligible: bool,
    mocked: bool,
    ingested_at: datetime,
) -> dict[str, Any]:
    if mocked:
        disclaimer = (
            "Mocked HTTP fixture. Not authenticated Alpaca market data and not a live quote. "
            "A 09:30 ET minute bar is a one-minute bucket, not a 09:30:00 print."
        )
    else:
        disclaimer = (
            "Read-only Alpaca historical GET. Not a live quote. "
            "A 09:30 ET minute bar is a one-minute bucket, not a guaranteed 09:30:00 print."
        )
    return {
        "vendor": "alpaca",
        "adapter_version": ADAPTER_VERSION,
        "retrieval_mode": RETRIEVAL_MOCKED_HTTP if mocked else RETRIEVAL_VENDOR_GET,
        "authenticated_vendor_data": not mocked,
        "feed": query.feed,
        "adjustment": query.adjustment,
        "currency": query.currency,
        "timeframe": query.timeframe,
        "symbol": query.symbol,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
        "host": ALPACA_DATA_HOST,
        "method": "GET",
        "path": _BARS_PATH.format(symbol=query.symbol),
        "ingested_at": ingested_at.isoformat(),
        "execution_eligible": eligible,
        "full_session_validated": any(
            bool(item.get("full_session_validated")) for item in qualities
        ),
        "missing_sessions": list(missing),
        "session_quality": qualities,
        "query_bounds": {
            "max_query_days": query.max_query_days,
            "max_pages": query.max_pages,
            "max_records": query.max_records,
        },
        "price_disclaimer": disclaimer,
    }


def _expected_minutes(session: NyseSession) -> list[datetime]:
    stamps: list[datetime] = []
    cursor = session.open
    last = session.close - timedelta(minutes=1)
    while cursor <= last:
        stamps.append(cursor)
        cursor += timedelta(minutes=1)
    return stamps


def redacted_query_url(query: AlpacaBarQuery) -> str:
    params = urlencode(
        {
            "timeframe": query.timeframe,
            "start": query.start.isoformat(),
            "end": query.end.isoformat(),
            "adjustment": query.adjustment,
            "feed": query.feed,
        }
    )
    return f"https://{ALPACA_DATA_HOST}/v2/stocks/{query.symbol}/bars?{params}"
