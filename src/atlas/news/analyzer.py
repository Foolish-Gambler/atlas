"""Deterministic scoring of caller-supplied headlines."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain import DataProvenance

_POSITIVE = frozenset({"beat", "growth", "upgrade", "record", "profit"})
_NEGATIVE = frozenset({"miss", "downgrade", "lawsuit", "fraud", "loss"})


@dataclass(frozen=True, slots=True)
class NewsItem:
    symbol: str
    headline: str
    provenance: DataProvenance = "caller_supplied"


@dataclass(frozen=True, slots=True)
class NewsScore:
    symbol: str
    score: float
    label: str
    provenance: DataProvenance
    headline_count: int


class NewsAnalyzer:
    """Keyword polarity over provided text. Does not fetch news."""

    def score(self, items: list[NewsItem]) -> NewsScore:
        if not items:
            raise ValueError("At least one news item is required.")
        symbol = items[0].symbol
        if any(item.symbol != symbol for item in items):
            raise ValueError("All news items must share the same symbol.")
        raw = 0
        for item in items:
            tokens = {token.strip(".,!?").lower() for token in item.headline.split()}
            raw += len(tokens & _POSITIVE)
            raw -= len(tokens & _NEGATIVE)
        bounded = max(-1.0, min(1.0, raw / max(len(items), 1)))
        if bounded > 0:
            label = "positive"
        elif bounded < 0:
            label = "negative"
        else:
            label = "neutral"
        return NewsScore(
            symbol=symbol,
            score=bounded,
            label=label,
            provenance="caller_supplied",
            headline_count=len(items),
        )
