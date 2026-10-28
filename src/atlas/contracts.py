"""Stable data contracts for ATLAS research and point-in-time event streams.

These types are additive. They do not replace `atlas.domain.Signal`,
`RiskDecision`, or `PaperBroker`. Ingestion adapters and the next backtest
engine should speak this language. Network I/O is intentionally absent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from atlas.validation import (
    InvalidInputError,
    normalize_symbol,
    require_aware_datetime,
    require_finite,
    require_int,
    require_non_negative_finite,
    require_positive_finite,
)


class SourceKind(StrEnum):
    SIMULATED = "simulated"
    CALLER_SUPPLIED = "caller_supplied"
    VENDOR_READ_ONLY = "vendor_read_only"


class StreamEventType(StrEnum):
    BAR_OPEN = "bar_open"
    BAR_CLOSE = "bar_close"
    QUOTE = "quote"
    NEWS = "news"
    FILING = "filing"
    STATEMENT = "statement"
    MACRO = "macro"
    RESEARCH = "research"
    PREDICTION = "prediction"


@dataclass(frozen=True, slots=True)
class Provenance:
    source_kind: SourceKind
    dataset_id: str
    revision: str
    vendor: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.revision.strip():
            raise InvalidInputError("dataset_id and revision are required.")
        if self.vendor == "":
            object.__setattr__(self, "vendor", None)


@dataclass(frozen=True, slots=True)
class TimeIndex:
    """Point-in-time stamps. `available_time` is when a strategy may use the fact."""

    event_time: datetime
    available_time: datetime
    ingested_time: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_time", require_aware_datetime(self.event_time, "event_time")
        )
        object.__setattr__(
            self,
            "available_time",
            require_aware_datetime(self.available_time, "available_time"),
        )
        if self.ingested_time is not None:
            object.__setattr__(
                self,
                "ingested_time",
                require_aware_datetime(self.ingested_time, "ingested_time"),
            )
        if self.available_time < self.event_time:
            raise InvalidInputError("available_time cannot precede event_time (lookahead).")
        if self.ingested_time is not None and self.ingested_time < self.available_time:
            raise InvalidInputError("ingested_time cannot precede available_time.")


@dataclass(frozen=True, slots=True)
class MarketBarRecord:
    """Daily (or session) bar with distinct open, close, and publication times.

    Complete OHLCV is usable only at `available_time` (>= `close_time`).
    Simulated fills use `open_time` and `open`, never `close_time`.
    """

    symbol: str
    open_time: datetime
    close_time: datetime
    available_time: datetime
    provenance: Provenance
    open: float
    high: float
    low: float
    close: float
    volume: int
    ingested_time: datetime | None = None
    vendor_time: datetime | None = None
    session_open_verified: bool = False
    session_close_verified: bool = False
    requires_open_interval_evidence: bool = False
    expected_open_time: datetime | None = None
    expected_close_time: datetime | None = None
    observed_open_time: datetime | None = None
    observed_last_time: datetime | None = None
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "open_time", require_aware_datetime(self.open_time, "open_time"))
        object.__setattr__(
            self, "close_time", require_aware_datetime(self.close_time, "close_time")
        )
        object.__setattr__(
            self, "available_time", require_aware_datetime(self.available_time, "available_time")
        )
        if self.close_time < self.open_time:
            raise InvalidInputError("close_time cannot precede open_time.")
        if self.available_time < self.close_time:
            raise InvalidInputError("available_time cannot precede close_time.")
        if self.ingested_time is not None:
            object.__setattr__(
                self,
                "ingested_time",
                require_aware_datetime(self.ingested_time, "ingested_time"),
            )
            if self.ingested_time < self.available_time:
                raise InvalidInputError("ingested_time cannot precede available_time.")
        if self.vendor_time is not None:
            object.__setattr__(
                self, "vendor_time", require_aware_datetime(self.vendor_time, "vendor_time")
            )
        if self.expected_open_time is not None:
            object.__setattr__(
                self,
                "expected_open_time",
                require_aware_datetime(self.expected_open_time, "expected_open_time"),
            )
        if self.expected_close_time is not None:
            object.__setattr__(
                self,
                "expected_close_time",
                require_aware_datetime(self.expected_close_time, "expected_close_time"),
            )
        if self.observed_open_time is not None:
            object.__setattr__(
                self,
                "observed_open_time",
                require_aware_datetime(self.observed_open_time, "observed_open_time"),
            )
        if self.observed_last_time is not None:
            object.__setattr__(
                self,
                "observed_last_time",
                require_aware_datetime(self.observed_last_time, "observed_last_time"),
            )
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        object.__setattr__(self, "open", require_positive_finite(self.open, "open"))
        object.__setattr__(self, "high", require_positive_finite(self.high, "high"))
        object.__setattr__(self, "low", require_positive_finite(self.low, "low"))
        object.__setattr__(self, "close", require_positive_finite(self.close, "close"))
        volume = require_int(self.volume, "volume")
        if volume < 0:
            raise InvalidInputError("volume must be non-negative.")
        object.__setattr__(self, "volume", volume)
        if self.high < self.low:
            raise InvalidInputError("high must be greater than or equal to low.")
        if self.high < self.open or self.high < self.close:
            raise InvalidInputError("high must be at least open and close.")
        if self.low > self.open or self.low > self.close:
            raise InvalidInputError("low must be at most open and close.")

    @property
    def ref_id(self) -> str:
        return f"bar:{self.symbol}:{self.close_time.isoformat()}"

    @property
    def open_event_id(self) -> str:
        return f"bar-open:{self.symbol}:{self.open_time.isoformat()}"

    @property
    def close_event_id(self) -> str:
        return f"bar-close:{self.symbol}:{self.close_time.isoformat()}"

    @property
    def open_index(self) -> TimeIndex:
        return TimeIndex(event_time=self.open_time, available_time=self.open_time)

    @property
    def close_index(self) -> TimeIndex:
        return TimeIndex(
            event_time=self.close_time,
            available_time=self.available_time,
            ingested_time=self.ingested_time,
        )


@dataclass(frozen=True, slots=True)
class FilingRecord:
    symbol: str
    form_type: str
    time: TimeIndex
    provenance: Provenance
    accession: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not self.form_type.strip() or not self.accession.strip():
            raise InvalidInputError("form_type and accession are required.")


@dataclass(frozen=True, slots=True)
class NewsRecord:
    symbol: str
    time: TimeIndex
    provenance: Provenance
    headline: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not self.headline.strip():
            raise InvalidInputError("headline is required.")


@dataclass(frozen=True, slots=True)
class StatementRecord:
    symbol: str
    time: TimeIndex
    provenance: Provenance
    earnings_per_share: float
    book_value_per_share: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "earnings_per_share",
            require_finite(self.earnings_per_share, "earnings_per_share"),
        )
        object.__setattr__(
            self,
            "book_value_per_share",
            require_finite(self.book_value_per_share, "book_value_per_share"),
        )


@dataclass(frozen=True, slots=True)
class MacroRecord:
    series_id: str
    time: TimeIndex
    provenance: Provenance
    value: float

    def __post_init__(self) -> None:
        if not self.series_id.strip():
            raise InvalidInputError("series_id is required.")
        object.__setattr__(self, "value", require_finite(self.value, "value"))


@dataclass(frozen=True, slots=True)
class ResearchArtifact:
    artifact_id: str
    model_id: str
    model_version: str
    time: TimeIndex
    provenance: Provenance
    symbols: tuple[str, ...]
    thesis: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.artifact_id.strip()
            or not self.model_id.strip()
            or not self.model_version.strip()
        ):
            raise InvalidInputError("artifact_id, model_id, and model_version are required.")
        object.__setattr__(
            self, "symbols", tuple(normalize_symbol(symbol) for symbol in self.symbols)
        )
        if not self.symbols:
            raise InvalidInputError("research artifacts must name at least one symbol.")


@dataclass(frozen=True, slots=True)
class ReturnPrediction:
    prediction_id: str
    model_id: str
    model_version: str
    symbol: str
    horizon: str
    expected_return: float
    uncertainty: float
    time: TimeIndex
    provenance: Provenance

    def __post_init__(self) -> None:
        if (
            not self.prediction_id.strip()
            or not self.model_id.strip()
            or not self.model_version.strip()
        ):
            raise InvalidInputError("prediction_id, model_id, and model_version are required.")
        if not self.horizon.strip():
            raise InvalidInputError("horizon is required.")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(
            self, "expected_return", require_finite(self.expected_return, "expected_return")
        )
        object.__setattr__(
            self, "uncertainty", require_non_negative_finite(self.uncertainty, "uncertainty")
        )


@dataclass(frozen=True, slots=True)
class EnsemblePrediction:
    ensemble_id: str
    symbol: str
    horizon: str
    expected_return: float
    uncertainty: float
    members: tuple[str, ...]
    time: TimeIndex
    provenance: Provenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not self.members:
            raise InvalidInputError("ensemble members are required.")
        object.__setattr__(
            self, "expected_return", require_finite(self.expected_return, "expected_return")
        )
        object.__setattr__(
            self, "uncertainty", require_non_negative_finite(self.uncertainty, "uncertainty")
        )


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event_id: str
    event_type: StreamEventType
    time: TimeIndex
    ref_id: str

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.ref_id.strip():
            raise InvalidInputError("event_id and ref_id are required.")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    run_id: str
    event_id: str
    risk_decision_id: str
    fill_id: int | None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.event_id.strip():
            raise InvalidInputError("run_id and event_id are required.")


class ReadOnlyBarSource(Protocol):
    """Local or future vendor adapter. Implementations must not place orders."""

    def bars(self, symbol: str) -> Sequence[MarketBarRecord]: ...


class ReadOnlyEventSource(Protocol):
    """Point-in-time event stream. Implementations must not place orders or call the network."""

    def stream(self) -> Sequence[StreamEvent]: ...


class PitReplaySource(Protocol):
    """Read-only market archive consumed by PitBacktestEngine. No orders, no HTTP."""

    def all_bars(self) -> tuple[MarketBarRecord, ...]: ...

    def stream(self) -> Sequence[StreamEvent]: ...

    def bar_by_ref(self, ref_id: str) -> MarketBarRecord: ...

    def filings(self) -> tuple[FilingRecord, ...]: ...

    def news(self) -> tuple[NewsRecord, ...]: ...

    def statements(self) -> tuple[StatementRecord, ...]: ...

    def research(self) -> tuple[ResearchArtifact, ...]: ...

    def predictions(self) -> tuple[ReturnPrediction, ...]: ...

    def ensembles(self) -> tuple[EnsemblePrediction, ...]: ...


_EVENT_RANK = {
    StreamEventType.BAR_OPEN: 0,
    StreamEventType.QUOTE: 1,
    StreamEventType.NEWS: 2,
    StreamEventType.FILING: 3,
    StreamEventType.STATEMENT: 4,
    StreamEventType.MACRO: 5,
    StreamEventType.RESEARCH: 6,
    StreamEventType.PREDICTION: 7,
    StreamEventType.BAR_CLOSE: 8,
}


def event_sort_key(event: StreamEvent) -> tuple[datetime, datetime, int, str]:
    return (
        event.time.available_time,
        event.time.event_time,
        _EVENT_RANK.get(event.event_type, 9),
        event.event_id,
    )


def sort_stream(events: Iterable[StreamEvent]) -> list[StreamEvent]:
    ordered = sorted(events, key=event_sort_key)
    previous: tuple[datetime, datetime, int, str] | None = None
    for event in ordered:
        key = event_sort_key(event)
        if previous is not None and key < previous:
            raise InvalidInputError("event stream is not monotonically ordered.")
        previous = key
    return ordered


class EventClock:
    """Cursor over a point-in-time event stream."""

    def __init__(self, events: Iterable[StreamEvent]) -> None:
        self._events = sort_stream(events)

    @property
    def events(self) -> tuple[StreamEvent, ...]:
        return tuple(self._events)

    def visible(self, as_of: datetime) -> list[StreamEvent]:
        as_of = require_aware_datetime(as_of, "as_of")
        return [event for event in self._events if event.time.available_time <= as_of]
