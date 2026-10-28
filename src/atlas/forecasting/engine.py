"""Candidate-signal generation. Forecasts never execute trades."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain import Signal, SignalSide
from atlas.fundamentals.analyzer import FundamentalScore
from atlas.news.analyzer import NewsScore
from atlas.validation import normalize_symbol, require_positive_finite


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    symbol: str
    last_price: float
    news: NewsScore | None = None
    fundamentals: FundamentalScore | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(
            self, "last_price", require_positive_finite(self.last_price, "last_price")
        )


class ForecastEngine:
    """Heuristic blend of provided research artifacts.

    Output is a Signal that still requires `RiskManager.evaluate`.
    """

    def propose(self, inputs: ForecastInputs) -> Signal:
        news_score = inputs.news.score if inputs.news is not None else 0.0
        fundamental_score = inputs.fundamentals.score if inputs.fundamentals is not None else 0.0
        combined = (news_score + fundamental_score) / 2.0
        if combined > 0.25:
            side = SignalSide.BUY
            quantity = 10
        elif combined < -0.25:
            side = SignalSide.SELL
            quantity = 10
        else:
            side = SignalSide.HOLD
            quantity = 0
        confidence = min(0.99, 0.5 + abs(combined) / 2.0)
        return Signal(
            symbol=inputs.symbol,
            side=side,
            quantity=quantity,
            reference_price=inputs.last_price,
            confidence=confidence,
            rationale="Heuristic blend of provided news and fundamental scores.",
            source="forecasting.heuristic",
        )
