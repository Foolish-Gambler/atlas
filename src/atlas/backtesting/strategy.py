"""Strategy protocol for PIT backtests. LLM/ML adapters should return Signals from artifacts."""

from __future__ import annotations

from typing import Protocol

from atlas.backtesting.view import MarketView
from atlas.domain import Signal, SignalSide


class Strategy(Protocol):
    def on_as_of(self, view: MarketView) -> Signal | None:
        """Return a candidate order using only `view`. Must not execute trades."""


class NextOpenBuyStrategy:
    """Baseline: after each visible close, buy a fixed size. Fill happens at the next open."""

    def __init__(self, quantity: int = 1, symbol: str | None = None) -> None:
        self.quantity = quantity
        self.symbol = symbol

    def on_as_of(self, view: MarketView) -> Signal | None:
        bar = view.last_bar(self.symbol)
        if bar is None:
            return None
        return Signal(
            symbol=bar.symbol,
            side=SignalSide.BUY,
            quantity=self.quantity,
            reference_price=bar.close,
            confidence=0.8,
            rationale="PIT baseline: queue buy after close for next open.",
            source="backtesting.pit.baseline",
        )
