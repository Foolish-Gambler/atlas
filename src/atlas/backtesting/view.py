"""Point-in-time market view shown to strategies."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from atlas.contracts import (
    EnsemblePrediction,
    FilingRecord,
    MarketBarRecord,
    NewsRecord,
    ResearchArtifact,
    ReturnPrediction,
    StatementRecord,
    StreamEvent,
)
from atlas.ingest.local import LocalFixtureSource
from atlas.validation import normalize_symbol, require_aware_datetime


@dataclass(frozen=True, slots=True)
class MarketView:
    """Information a strategy may use at `as_of`. Future artifacts are omitted."""

    as_of: datetime
    events: tuple[StreamEvent, ...]
    bars: tuple[MarketBarRecord, ...]
    filings: tuple[FilingRecord, ...]
    news: tuple[NewsRecord, ...]
    statements: tuple[StatementRecord, ...]
    research: tuple[ResearchArtifact, ...]
    predictions: tuple[ReturnPrediction, ...]
    ensembles: tuple[EnsemblePrediction, ...]

    def last_bar(self, symbol: str | None = None) -> MarketBarRecord | None:
        rows = self.bars
        if symbol is not None:
            key = normalize_symbol(symbol)
            rows = tuple(bar for bar in rows if bar.symbol == key)
        if not rows:
            return None
        return rows[-1]


def build_view(
    source: LocalFixtureSource,
    visible: Sequence[StreamEvent],
    as_of: datetime,
) -> MarketView:
    as_of = require_aware_datetime(as_of, "as_of")
    visible_ids = {event.event_id for event in visible}
    events = tuple(event for event in visible if event.time.available_time <= as_of)
    bars = tuple(
        sorted(
            (
                bar
                for bar in source.all_bars()
                if bar.close_event_id in visible_ids and bar.available_time <= as_of
            ),
            key=lambda bar: (bar.close_time, bar.symbol),
        )
    )
    return MarketView(
        as_of=as_of,
        events=events,
        bars=bars,
        filings=tuple(
            filing
            for filing in source.filings()
            if f"filing:{filing.accession}" in visible_ids and filing.time.available_time <= as_of
        ),
        news=tuple(
            item
            for item in source.news()
            if f"news:{item.symbol}:{item.time.event_time.isoformat()}" in visible_ids
        ),
        statements=tuple(
            statement
            for statement in source.statements()
            if f"statement:{statement.symbol}:{statement.time.event_time.isoformat()}"
            in visible_ids
        ),
        research=tuple(
            artifact
            for artifact in source.research()
            if f"research:{artifact.artifact_id}" in visible_ids
            and artifact.time.available_time <= as_of
        ),
        predictions=tuple(
            prediction
            for prediction in source.predictions()
            if f"prediction:{prediction.prediction_id}" in visible_ids
            and prediction.time.available_time <= as_of
        ),
        ensembles=tuple(
            ensemble
            for ensemble in source.ensembles()
            if f"ensemble:{ensemble.ensemble_id}" in visible_ids
        ),
    )
