"""Read-only local fixture ingestion. No network, no live data."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
    XbrlFactRecord,
)
from atlas.ingest.codec import (
    build_stream,
    parse_bar,
    parse_ensemble,
    parse_fact,
    parse_filing,
    parse_news,
    parse_prediction,
    parse_research,
    parse_statement,
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
        self._bars = tuple(_parse_many(payload.get("bars", []), parse_bar, self.provenance))
        self._filings = tuple(
            _parse_many(payload.get("filings", []), parse_filing, self.provenance)
        )
        self._news = tuple(_parse_many(payload.get("news", []), parse_news, self.provenance))
        self._statements = tuple(
            _parse_many(payload.get("statements", []), parse_statement, self.provenance)
        )
        self._research = tuple(
            _parse_many(payload.get("research", []), parse_research, self.provenance)
        )
        self._predictions = tuple(
            _parse_many(payload.get("predictions", []), parse_prediction, self.provenance)
        )
        self._ensembles = tuple(
            _parse_many(payload.get("ensembles", []), parse_ensemble, self.provenance)
        )
        self._facts = tuple(_parse_many(payload.get("facts", []), parse_fact, self.provenance))
        self._by_ref = {bar.ref_id: bar for bar in self._bars}
        self._events = build_stream(
            self._bars,
            self._filings,
            self._news,
            self._statements,
            self._research,
            self._predictions,
            self._ensembles,
            self._facts,
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

    def facts(self) -> tuple[XbrlFactRecord, ...]:
        return self._facts

    def stream(self) -> Sequence[StreamEvent]:
        return self._events

    def bar_by_ref(self, ref_id: str) -> MarketBarRecord:
        try:
            return self._by_ref[ref_id]
        except KeyError as exc:
            raise InvalidInputError(f"unknown bar ref_id {ref_id!r}") from exc


def _parse_many(rows: list[dict[str, Any]], parser: Any, provenance: Provenance) -> list[Any]:
    return [parser(row, provenance, require_ingested=False) for row in rows]
