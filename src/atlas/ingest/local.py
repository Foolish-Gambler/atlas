"""Read-only local fixture ingestion. No network, no live data."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.contracts import (
    EnsemblePrediction,
    FilingRecord,
    MarketBarRecord,
    NewsRecord,
    Provenance,
    ReadOnlyBarSource,
    ReadOnlyEventSource,
    ResearchArtifact,
    ReturnPrediction,
    SourceKind,
    StatementRecord,
    StreamEvent,
    StreamEventType,
    TimeIndex,
)
from atlas.validation import InvalidInputError, normalize_symbol


class LocalFixtureSource(ReadOnlyBarSource, ReadOnlyEventSource):
    """Load simulated or caller-supplied JSON fixtures from disk."""

    def __init__(self, path: Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        kind = SourceKind(payload.get("source_kind", "simulated"))
        if kind is SourceKind.VENDOR_READ_ONLY:
            raise InvalidInputError("local fixtures cannot claim vendor_read_only provenance.")
        self.provenance = Provenance(
            source_kind=kind,
            dataset_id=str(payload["dataset_id"]),
            revision=str(payload["revision"]),
        )
        self._bars = tuple(_parse_bars(payload.get("bars", []), self.provenance))
        self._filings = tuple(_parse_filings(payload.get("filings", []), self.provenance))
        self._news = tuple(_parse_news(payload.get("news", []), self.provenance))
        self._statements = tuple(_parse_statements(payload.get("statements", []), self.provenance))
        self._research = tuple(_parse_research(payload.get("research", []), self.provenance))
        self._predictions = tuple(
            _parse_predictions(payload.get("predictions", []), self.provenance)
        )
        self._ensembles = tuple(_parse_ensembles(payload.get("ensembles", []), self.provenance))
        self._events = _build_stream(
            self._bars,
            self._filings,
            self._news,
            self._statements,
            self._research,
            self._predictions,
            self._ensembles,
        )

    def bars(self, symbol: str) -> Sequence[MarketBarRecord]:
        key = normalize_symbol(symbol)
        return tuple(bar for bar in self._bars if bar.symbol == key)

    def all_bars(self) -> tuple[MarketBarRecord, ...]:
        return self._bars

    def filings(self) -> tuple[FilingRecord, ...]:
        return self._filings

    def news(self) -> tuple[NewsRecord, ...]:
        return self._news

    def statements(self) -> tuple[StatementRecord, ...]:
        return self._statements

    def research(self) -> tuple[ResearchArtifact, ...]:
        return self._research

    def predictions(self) -> tuple[ReturnPrediction, ...]:
        return self._predictions

    def ensembles(self) -> tuple[EnsemblePrediction, ...]:
        return self._ensembles

    def stream(self) -> Sequence[StreamEvent]:
        return self._events


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _time(row: dict[str, Any]) -> TimeIndex:
    return TimeIndex(
        event_time=_parse_dt(row["event_time"]),
        available_time=_parse_dt(row["available_time"]),
        ingested_time=_parse_dt(row["ingested_time"]) if row.get("ingested_time") else None,
    )


def _parse_bars(rows: list[dict[str, Any]], provenance: Provenance) -> list[MarketBarRecord]:
    return [
        MarketBarRecord(
            symbol=row["symbol"],
            time=_time(row),
            provenance=provenance,
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )
        for row in rows
    ]


def _parse_filings(rows: list[dict[str, Any]], provenance: Provenance) -> list[FilingRecord]:
    return [
        FilingRecord(
            symbol=row["symbol"],
            form_type=row["form_type"],
            time=_time(row),
            provenance=provenance,
            accession=row["accession"],
        )
        for row in rows
    ]


def _parse_news(rows: list[dict[str, Any]], provenance: Provenance) -> list[NewsRecord]:
    return [
        NewsRecord(
            symbol=row["symbol"],
            time=_time(row),
            provenance=provenance,
            headline=row["headline"],
        )
        for row in rows
    ]


def _parse_statements(rows: list[dict[str, Any]], provenance: Provenance) -> list[StatementRecord]:
    return [
        StatementRecord(
            symbol=row["symbol"],
            time=_time(row),
            provenance=provenance,
            earnings_per_share=row["earnings_per_share"],
            book_value_per_share=row["book_value_per_share"],
        )
        for row in rows
    ]


def _parse_research(rows: list[dict[str, Any]], provenance: Provenance) -> list[ResearchArtifact]:
    return [
        ResearchArtifact(
            artifact_id=row["artifact_id"],
            model_id=row["model_id"],
            model_version=row["model_version"],
            time=_time(row),
            provenance=provenance,
            symbols=tuple(row["symbols"]),
            thesis=row["thesis"],
            evidence_refs=tuple(row.get("evidence_refs", ())),
        )
        for row in rows
    ]


def _parse_predictions(
    rows: list[dict[str, Any]], provenance: Provenance
) -> list[ReturnPrediction]:
    return [
        ReturnPrediction(
            prediction_id=row["prediction_id"],
            model_id=row["model_id"],
            model_version=row["model_version"],
            symbol=row["symbol"],
            horizon=row["horizon"],
            expected_return=row["expected_return"],
            uncertainty=row["uncertainty"],
            time=_time(row),
            provenance=provenance,
        )
        for row in rows
    ]


def _parse_ensembles(
    rows: list[dict[str, Any]], provenance: Provenance
) -> list[EnsemblePrediction]:
    return [
        EnsemblePrediction(
            ensemble_id=row["ensemble_id"],
            symbol=row["symbol"],
            horizon=row["horizon"],
            expected_return=row["expected_return"],
            uncertainty=row["uncertainty"],
            members=tuple(row["members"]),
            time=_time(row),
            provenance=provenance,
        )
        for row in rows
    ]


def _build_stream(
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
        event_id = f"bar:{bar.symbol}:{bar.time.event_time.isoformat()}"
        events.append(
            StreamEvent(event_id, StreamEventType.BAR, bar.time, event_id),
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
