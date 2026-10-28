"""In-memory paper broker. Refuses live connectivity."""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.config import AtlasSettings
from atlas.domain import PortfolioSnapshot, Signal, SignalSide, TradingMode
from atlas.risk.manager import RiskDecision
from atlas.safety import SafetyError, assert_no_live_broker, assert_paper_only


@dataclass(frozen=True, slots=True)
class PaperFill:
    symbol: str
    side: SignalSide
    quantity: int
    price: float
    simulated: bool = True


@dataclass
class PaperBroker:
    settings: AtlasSettings
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        assert_paper_only(self.settings)
        assert_no_live_broker(self.settings.live_broker_connected)
        if self.settings.trading_mode is not TradingMode.PAPER:
            raise SafetyError("PaperBroker requires paper trading mode.")

    @classmethod
    def from_settings(cls, settings: AtlasSettings | None = None) -> PaperBroker:
        resolved = settings or AtlasSettings.load()
        return cls(settings=resolved, cash=resolved.starting_cash)

    @property
    def mode(self) -> TradingMode:
        return TradingMode.PAPER

    def snapshot(self) -> PortfolioSnapshot:
        equity = self.cash
        return PortfolioSnapshot(cash=self.cash, positions=dict(self.positions), equity=equity)

    def submit(self, decision: RiskDecision) -> PaperFill:
        assert_paper_only(self.settings)
        if not decision.approved:
            raise SafetyError(f"Signal rejected by risk: {decision.summary}")
        signal: Signal = decision.signal
        notional = signal.quantity * signal.reference_price
        if signal.side is SignalSide.BUY:
            self.cash -= notional
            self.positions[signal.symbol] = self.positions.get(signal.symbol, 0) + signal.quantity
        elif signal.side is SignalSide.SELL:
            self.cash += notional
            remaining = self.positions.get(signal.symbol, 0) - signal.quantity
            if remaining:
                self.positions[signal.symbol] = remaining
            else:
                self.positions.pop(signal.symbol, None)
        else:
            raise SafetyError("Hold signals cannot be submitted.")
        fill = PaperFill(
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.reference_price,
            simulated=True,
        )
        self.fills.append(fill)
        return fill
