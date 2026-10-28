"""$10,000 virtual paper demo. Mocked or snapshot prices are never live quotes."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.backtesting.costs import ExecutionModel
from atlas.backtesting.pit import PitBacktestEngine, PitBacktestResult
from atlas.backtesting.strategy import Strategy
from atlas.backtesting.view import MarketView
from atlas.config import AtlasSettings
from atlas.domain import Signal, SignalSide, TradingMode
from atlas.ingest.snapshot import SnapshotSource
from atlas.paper_trading.report import PaperPortfolioReport, report_from_fills
from atlas.risk.manager import RiskManager
from atlas.safety import SafetyError

VIRTUAL_CASH_USD = 10_000.0
_DEMO_DISCLAIMER = (
    "Paper simulation only. Snapshot prices may be mocked HTTP fixtures and are "
    "not live or authoritative historical market data."
)


class ExecutionUnsuitableError(SafetyError):
    """Snapshot lacks verified RTH session opens and cannot be used to fill."""


class BuyOnceNextOpenStrategy:
    """Deterministic demo: after the first completed bar, buy a fixed size once."""

    def __init__(self, quantity: int = 1) -> None:
        self.quantity = quantity
        self._sent = False

    def on_as_of(self, view: MarketView) -> Signal | None:
        if self._sent:
            return None
        bar = view.last_bar()
        if bar is None:
            return None
        self._sent = True
        return Signal(
            symbol=bar.symbol,
            side=SignalSide.BUY,
            quantity=self.quantity,
            reference_price=bar.close,
            confidence=0.8,
            rationale="Virtual $10,000 demo: one next-open buy after the first close.",
            source="paper_trading.demo",
        )


@dataclass(frozen=True, slots=True)
class VirtualPortfolioDemo:
    result: PitBacktestResult
    report: PaperPortfolioReport


def run_virtual_portfolio(
    source: SnapshotSource,
    *,
    starting_cash: float = VIRTUAL_CASH_USD,
    quantity: int = 1,
    execution: ExecutionModel | None = None,
    risk: RiskManager | None = None,
    run_id: str = "virtual-10k",
    strategy: Strategy | None = None,
) -> VirtualPortfolioDemo:
    if not source.manifest.execution_eligible:
        raise ExecutionUnsuitableError(
            "snapshot is unsuitable for execution backtests: RTH session opens "
            "were not verified from 1Min bars."
        )
    settings = AtlasSettings(
        trading_mode=TradingMode.PAPER,
        starting_cash=starting_cash,
        live_broker_connected=False,
    )
    engine = PitBacktestEngine(
        settings=settings,
        risk=risk or RiskManager(),
        execution=execution or ExecutionModel(),
    )
    result = engine.run(
        source,
        strategy or BuyOnceNextOpenStrategy(quantity=quantity),
        run_id=run_id,
    )
    fills = [row for row in result.journal.records if row["type"] == "fill"]
    disclaimer = str(source.manifest.metadata.get("price_disclaimer", _DEMO_DISCLAIMER))
    report = report_from_fills(
        result,
        fills,
        execution_eligible=True,
        price_disclaimer=disclaimer,
    )
    return VirtualPortfolioDemo(result=result, report=report)
