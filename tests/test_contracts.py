from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas.contracts import (
    EventClock,
    FilingRecord,
    MarketBarRecord,
    Provenance,
    ResearchArtifact,
    ReturnPrediction,
    SourceKind,
    StreamEvent,
    StreamEventType,
    TimeIndex,
)
from atlas.validation import InvalidInputError


def _t(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        source_kind=SourceKind.SIMULATED,
        dataset_id="fixture-bars",
        revision="1",
    )


def test_time_index_rejects_lookahead() -> None:
    with pytest.raises(InvalidInputError, match="lookahead"):
        TimeIndex(event_time=_t(2), available_time=_t(1))


def test_time_index_rejects_naive_datetimes() -> None:
    with pytest.raises(InvalidInputError, match="timezone-aware"):
        TimeIndex(event_time=datetime(2024, 1, 1), available_time=_t(1))


def test_event_clock_hides_future_available_times() -> None:
    early = StreamEvent(
        event_id="e1",
        event_type=StreamEventType.BAR_CLOSE,
        time=TimeIndex(event_time=_t(1, 16), available_time=_t(1, 16)),
        ref_id="bar-1",
    )
    filing = StreamEvent(
        event_id="e2",
        event_type=StreamEventType.FILING,
        time=TimeIndex(event_time=_t(1), available_time=_t(3, 21)),
        ref_id="10-k",
    )
    clock = EventClock([filing, early])
    visible = clock.visible(_t(2))
    assert [item.event_id for item in visible] == ["e1"]
    assert [item.event_id for item in clock.visible(_t(4))] == ["e1", "e2"]


def test_market_bar_record_validates_ohlc() -> None:
    with pytest.raises(InvalidInputError):
        MarketBarRecord(
            symbol="sim-a",
            open_time=_t(2, 14),
            close_time=_t(2, 21),
            available_time=_t(2, 21),
            provenance=_prov(),
            open=10.0,
            high=9.0,
            low=8.0,
            close=9.5,
            volume=1,
        )


def test_market_bar_requires_open_close_available_order() -> None:
    with pytest.raises(InvalidInputError, match="close_time"):
        MarketBarRecord(
            symbol="sim-a",
            open_time=_t(2, 21),
            close_time=_t(2, 14),
            available_time=_t(2, 21),
            provenance=_prov(),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1,
        )
    with pytest.raises(InvalidInputError, match="available_time"):
        MarketBarRecord(
            symbol="sim-a",
            open_time=_t(2, 14),
            close_time=_t(2, 21),
            available_time=_t(2, 16),
            provenance=_prov(),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1,
        )


def test_filing_separates_period_end_from_available_time() -> None:
    record = FilingRecord(
        symbol="sim-a",
        form_type="10-K",
        time=TimeIndex(event_time=_t(1), available_time=_t(1) + timedelta(days=40)),
        provenance=_prov(),
        accession="000-test",
    )
    assert record.symbol == "SIM-A"
    assert record.time.available_time > record.time.event_time


def test_research_and_prediction_require_model_version() -> None:
    time = TimeIndex(event_time=_t(2), available_time=_t(2))
    artifact = ResearchArtifact(
        artifact_id="r1",
        model_id="research.heuristic",
        model_version="0.1.0",
        time=time,
        provenance=_prov(),
        symbols=("sim-a",),
        thesis="Caller-supplied fixture thesis.",
    )
    pred = ReturnPrediction(
        prediction_id="p1",
        model_id="quant.baseline",
        model_version="0.1.0",
        symbol="sim-a",
        horizon="5d",
        expected_return=0.01,
        uncertainty=0.02,
        time=time,
        provenance=_prov(),
    )
    assert artifact.symbols == ("SIM-A",)
    assert pred.uncertainty == 0.02
    with pytest.raises(InvalidInputError):
        ReturnPrediction(
            prediction_id="p2",
            model_id="quant.baseline",
            model_version="0.1.0",
            symbol="sim-a",
            horizon="5d",
            expected_return=0.01,
            uncertainty=-0.01,
            time=time,
            provenance=_prov(),
        )
