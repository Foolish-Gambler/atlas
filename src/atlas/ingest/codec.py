"""Serialize and parse snapshot/fixture records. No network I/O."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from atlas.contracts import (
    EnsemblePrediction,
    FilingRecord,
    MarketBarRecord,
    NewsRecord,
    Provenance,
    ResearchArtifact,
    ReturnPrediction,
    StatementRecord,
    StreamEvent,
    StreamEventType,
    TimeIndex,
)
from atlas.validation import InvalidInputError


def parse_datetime(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{name} must be an ISO-8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInputError(f"{name} is not a valid datetime.") from exc
    return parsed


def require_time_index(row: dict[str, Any], *, require_ingested: bool) -> TimeIndex:
    ingested_raw = row.get("ingested_time")
    if require_ingested and not ingested_raw:
        raise InvalidInputError("ingested_time is required on snapshot records.")
    return TimeIndex(
        event_time=parse_datetime(row["event_time"], "event_time"),
        available_time=parse_datetime(row["available_time"], "available_time"),
        ingested_time=parse_datetime(ingested_raw, "ingested_time") if ingested_raw else None,
    )


def bar_to_row(bar: MarketBarRecord) -> dict[str, Any]:
    if bar.ingested_time is None:
        raise InvalidInputError("snapshot bars require ingested_time.")
    return {
        "symbol": bar.symbol,
        "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(),
        "available_time": bar.available_time.isoformat(),
        "ingested_time": bar.ingested_time.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "session_open_verified": bar.session_open_verified,
        "requires_open_interval_evidence": bar.requires_open_interval_evidence,
        "vendor_time": bar.vendor_time.isoformat() if bar.vendor_time is not None else None,
        "expected_open_time": (
            bar.expected_open_time.isoformat() if bar.expected_open_time is not None else None
        ),
        "observed_open_time": (
            bar.observed_open_time.isoformat() if bar.observed_open_time is not None else None
        ),
        "quality_flags": list(bar.quality_flags),
    }


def parse_bar(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> MarketBarRecord:
    ingested_raw = row.get("ingested_time")
    if require_ingested and not ingested_raw:
        raise InvalidInputError("ingested_time is required on snapshot bars.")
    return MarketBarRecord(
        symbol=row["symbol"],
        open_time=parse_datetime(row["open_time"], "open_time"),
        close_time=parse_datetime(row["close_time"], "close_time"),
        available_time=parse_datetime(row["available_time"], "available_time"),
        provenance=provenance,
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        ingested_time=parse_datetime(ingested_raw, "ingested_time") if ingested_raw else None,
        vendor_time=(
            parse_datetime(row["vendor_time"], "vendor_time") if row.get("vendor_time") else None
        ),
        session_open_verified=bool(row.get("session_open_verified", False)),
        requires_open_interval_evidence=bool(row.get("requires_open_interval_evidence", False)),
        expected_open_time=(
            parse_datetime(row["expected_open_time"], "expected_open_time")
            if row.get("expected_open_time")
            else None
        ),
        observed_open_time=(
            parse_datetime(row["observed_open_time"], "observed_open_time")
            if row.get("observed_open_time")
            else None
        ),
        quality_flags=tuple(row.get("quality_flags") or ()),
    )


def time_to_row(time: TimeIndex) -> dict[str, str]:
    if time.ingested_time is None:
        raise InvalidInputError("snapshot records require ingested_time.")
    return {
        "event_time": time.event_time.isoformat(),
        "available_time": time.available_time.isoformat(),
        "ingested_time": time.ingested_time.isoformat(),
    }


def filing_to_row(record: FilingRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "form_type": record.form_type,
        "accession": record.accession,
        **time_to_row(record.time),
    }


def parse_filing(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> FilingRecord:
    return FilingRecord(
        symbol=row["symbol"],
        form_type=row["form_type"],
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
        accession=row["accession"],
    )


def news_to_row(record: NewsRecord) -> dict[str, Any]:
    return {"symbol": record.symbol, "headline": record.headline, **time_to_row(record.time)}


def parse_news(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> NewsRecord:
    return NewsRecord(
        symbol=row["symbol"],
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
        headline=row["headline"],
    )


def statement_to_row(record: StatementRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "earnings_per_share": record.earnings_per_share,
        "book_value_per_share": record.book_value_per_share,
        "period_end": record.time.event_time.isoformat(),
        **time_to_row(record.time),
    }


def parse_statement(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> StatementRecord:
    return StatementRecord(
        symbol=row["symbol"],
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
        earnings_per_share=row["earnings_per_share"],
        book_value_per_share=row["book_value_per_share"],
    )


def research_to_row(record: ResearchArtifact) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "model_id": record.model_id,
        "model_version": record.model_version,
        "symbols": list(record.symbols),
        "thesis": record.thesis,
        "evidence_refs": list(record.evidence_refs),
        **time_to_row(record.time),
    }


def parse_research(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> ResearchArtifact:
    return ResearchArtifact(
        artifact_id=row["artifact_id"],
        model_id=row["model_id"],
        model_version=row["model_version"],
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
        symbols=tuple(row["symbols"]),
        thesis=row["thesis"],
        evidence_refs=tuple(row.get("evidence_refs", ())),
    )


def prediction_to_row(record: ReturnPrediction) -> dict[str, Any]:
    return {
        "prediction_id": record.prediction_id,
        "model_id": record.model_id,
        "model_version": record.model_version,
        "symbol": record.symbol,
        "horizon": record.horizon,
        "expected_return": record.expected_return,
        "uncertainty": record.uncertainty,
        **time_to_row(record.time),
    }


def parse_prediction(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> ReturnPrediction:
    return ReturnPrediction(
        prediction_id=row["prediction_id"],
        model_id=row["model_id"],
        model_version=row["model_version"],
        symbol=row["symbol"],
        horizon=row["horizon"],
        expected_return=row["expected_return"],
        uncertainty=row["uncertainty"],
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
    )


def ensemble_to_row(record: EnsemblePrediction) -> dict[str, Any]:
    return {
        "ensemble_id": record.ensemble_id,
        "symbol": record.symbol,
        "horizon": record.horizon,
        "expected_return": record.expected_return,
        "uncertainty": record.uncertainty,
        "members": list(record.members),
        **time_to_row(record.time),
    }


def parse_ensemble(
    row: dict[str, Any], provenance: Provenance, *, require_ingested: bool
) -> EnsemblePrediction:
    return EnsemblePrediction(
        ensemble_id=row["ensemble_id"],
        symbol=row["symbol"],
        horizon=row["horizon"],
        expected_return=row["expected_return"],
        uncertainty=row["uncertainty"],
        members=tuple(row["members"]),
        time=require_time_index(row, require_ingested=require_ingested),
        provenance=provenance,
    )


def dumps_canonical(payload: Any) -> bytes:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def build_stream(
    bars: Sequence[MarketBarRecord],
    filings: Sequence[FilingRecord],
    news: Sequence[NewsRecord],
    statements: Sequence[StatementRecord],
    research: Sequence[ResearchArtifact],
    predictions: Sequence[ReturnPrediction],
    ensembles: Sequence[EnsemblePrediction],
) -> tuple[StreamEvent, ...]:
    events: list[StreamEvent] = []
    for bar in bars:
        events.append(
            StreamEvent(bar.open_event_id, StreamEventType.BAR_OPEN, bar.open_index, bar.ref_id)
        )
        events.append(
            StreamEvent(bar.close_event_id, StreamEventType.BAR_CLOSE, bar.close_index, bar.ref_id)
        )
    for filing in filings:
        event_id = f"filing:{filing.accession}"
        events.append(StreamEvent(event_id, StreamEventType.FILING, filing.time, event_id))
    for item in news:
        event_id = f"news:{item.symbol}:{item.time.event_time.isoformat()}"
        events.append(StreamEvent(event_id, StreamEventType.NEWS, item.time, event_id))
    for statement in statements:
        event_id = f"statement:{statement.symbol}:{statement.time.event_time.isoformat()}"
        events.append(StreamEvent(event_id, StreamEventType.STATEMENT, statement.time, event_id))
    for artifact in research:
        events.append(
            StreamEvent(
                f"research:{artifact.artifact_id}",
                StreamEventType.RESEARCH,
                artifact.time,
                artifact.artifact_id,
            )
        )
    for prediction in predictions:
        events.append(
            StreamEvent(
                f"prediction:{prediction.prediction_id}",
                StreamEventType.PREDICTION,
                prediction.time,
                prediction.prediction_id,
            )
        )
    for ensemble in ensembles:
        events.append(
            StreamEvent(
                f"ensemble:{ensemble.ensemble_id}",
                StreamEventType.PREDICTION,
                ensemble.time,
                ensemble.ensemble_id,
            )
        )
    return tuple(events)
