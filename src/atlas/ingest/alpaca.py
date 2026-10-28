"""Read-only Alpaca historical bars. No order routing, no live brokerage."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from atlas.contracts import MarketBarRecord, Provenance, SourceKind
from atlas.ingest.http import ALPACA_DATA_HOST, ReadOnlyHttp
from atlas.ingest.snapshot import SnapshotManifest, SnapshotPayload, SnapshotStore
from atlas.market_data.nyse import EASTERN, NyseCalendar
from atlas.validation import InvalidInputError, normalize_symbol, require_finite

ADAPTER_VERSION = "atlas.alpaca.bars.v1"
DEFAULT_SYMBOL = "AAPL"
DEFAULT_START = date(2024, 1, 1)
DEFAULT_END = date(2025, 12, 31)
DEFAULT_FEED = "iex"
DEFAULT_ADJUSTMENT = "raw"
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
    simulated: bool = False


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
        path = _BARS_PATH.format(symbol=query.symbol)
        url = f"https://{ALPACA_DATA_HOST}{path}"
        while True:
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
            token = payload.get("next_page_token")
            if not token:
                break
            page_token = str(token)
        return rows

    def ingest(
        self,
        store: SnapshotStore,
        *,
        dataset_id: str,
        revision: str,
        query: AlpacaBarQuery | None = None,
        created_at: datetime | None = None,
    ) -> AlpacaIngestResult:
        query = query or AlpacaBarQuery()
        ingested_at = created_at or datetime.now(UTC)
        raw_rows = self.fetch_bars(query)
        raw = [_parse_vendor_bar(row) for row in raw_rows]
        provenance = Provenance(
            source_kind=SourceKind.VENDOR_READ_ONLY,
            dataset_id=dataset_id,
            revision=revision,
            vendor="alpaca",
        )
        if query.timeframe == "1Min":
            bars, missing, eligible = _from_minutes(
                raw, query, provenance, ingested_at, self.calendar
            )
        else:
            bars, missing, eligible = _from_daily(
                raw, query, provenance, ingested_at, self.calendar
            )
        metadata = {
            "vendor": "alpaca",
            "adapter_version": ADAPTER_VERSION,
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
            "execution_eligible": eligible,
            "missing_sessions": list(missing),
            "price_disclaimer": (
                "Vendor historical bars ingested through a read-only adapter. "
                "Mocked HTTP responses are not actual historical prices."
            ),
        }
        manifest = store.create(
            dataset_id,
            revision,
            SnapshotPayload(bars=tuple(bars)),
            source_kind=SourceKind.VENDOR_READ_ONLY,
            vendor="alpaca",
            created_at=ingested_at,
            metadata=metadata,
        )
        return AlpacaIngestResult(
            manifest=manifest,
            bars=tuple(bars),
            missing_sessions=missing,
            execution_eligible=eligible,
            simulated=False,
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
) -> tuple[list[MarketBarRecord], tuple[str, ...], bool]:
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
    for day, bar in sorted(by_session.items()):
        session = calendar.session_on(day)
        assert session is not None
        records.append(
            MarketBarRecord(
                symbol=query.symbol,
                open_time=session.open,
                close_time=session.close,
                available_time=session.close,
                provenance=provenance,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                ingested_time=ingested_at,
                vendor_time=bar.vendor_time,
                session_open_verified=False,
            )
        )
    return records, missing, False


def _from_minutes(
    raw: Sequence[_RawBar],
    query: AlpacaBarQuery,
    provenance: Provenance,
    ingested_at: datetime,
    calendar: NyseCalendar,
) -> tuple[list[MarketBarRecord], tuple[str, ...], bool]:
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
    for day, rows in sorted(grouped.items()):
        session = calendar.session_on(day)
        assert session is not None
        ordered = sorted(rows, key=lambda item: item.vendor_time)
        times = [item.vendor_time for item in ordered]
        if len(times) != len(set(times)):
            raise AlpacaIntegrityError(f"duplicate minute bars for {day.isoformat()}")
        first, last = ordered[0], ordered[-1]
        records.append(
            MarketBarRecord(
                symbol=query.symbol,
                open_time=first.vendor_time.astimezone(EASTERN),
                close_time=session.close,
                available_time=session.close,
                provenance=provenance,
                open=first.open,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=last.close,
                volume=sum(item.volume for item in ordered),
                ingested_time=ingested_at,
                vendor_time=first.vendor_time,
                session_open_verified=True,
            )
        )
    return records, missing, True


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
