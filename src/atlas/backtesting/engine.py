"""Legacy same-bar replay. Unsafe for research because of look-ahead bias.

Use `PitBacktestEngine` for point-in-time research backtests. This engine
signals and fills at the same bar close, so it can see information that would
not have been available at a tradable open.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from atlas.config import AtlasSettings
from atlas.domain import PriceMark, Signal, SignalSide
from atlas.market_data.models import Bar
from atlas.paper_trading.broker import PaperBroker
from atlas.risk.manager import RiskManager

_LOOKAHEAD_MESSAGE = (
    "BacktestEngine is unsafe for research backtesting: it fills at the same "
    "bar close used to form the signal (look-ahead bias). Use PitBacktestEngine."
)


class LookaheadBiasWarning(UserWarning):
    """Raised when the legacy close-to-close backtest path is used."""


@dataclass(frozen=True, slots=True)
class BacktestResult:
    starting_cash: float
    ending_cash: float
    ending_equity: float
    fills: int
    rejected: int
    provenance_ok: bool


class BacktestEngine:
    """Legacy close-to-close replay. Not valid for research or strategy evaluation."""

    LOOKAHEAD_BIAS = True

    def __init__(
        self,
        settings: AtlasSettings | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.settings = settings or AtlasSettings.load()
        self.risk = risk or RiskManager()

    def run(self, bars: list[Bar], quantity: int = 1) -> BacktestResult:
        warnings.warn(_LOOKAHEAD_MESSAGE, LookaheadBiasWarning, stacklevel=2)
        if not bars:
            raise ValueError("bars are required.")
        allowed = {"simulated", "caller_supplied"}
        if any(bar.provenance not in allowed for bar in bars):
            raise ValueError("bars must declare provenance.")
        broker = PaperBroker.from_settings(self.settings, risk=self.risk)
        rejected = 0
        symbol = bars[0].symbol
        if any(bar.symbol != symbol for bar in bars):
            raise ValueError("all bars must share the same symbol.")
        for bar in bars:
            broker.update_marks(
                {
                    symbol: PriceMark(
                        symbol=symbol,
                        price=bar.close,
                        as_of=bar.timestamp,
                        provenance=bar.provenance,
                    )
                }
            )
            signal = Signal(
                symbol=symbol,
                side=SignalSide.BUY,
                quantity=quantity,
                reference_price=bar.close,
                confidence=0.8,
                rationale="Backtest path following provided closes.",
                source="backtesting",
            )
            decision = self.risk.evaluate(signal, broker.snapshot())
            if decision.approved:
                broker.submit(decision)
            else:
                rejected += 1
        return BacktestResult(
            starting_cash=self.settings.starting_cash,
            ending_cash=broker.cash,
            ending_equity=broker.marked_equity(),
            fills=len(broker.fills),
            rejected=rejected,
            provenance_ok=all(bar.provenance in allowed for bar in bars),
        )
