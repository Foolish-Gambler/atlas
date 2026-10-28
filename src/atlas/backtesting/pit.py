"""Point-in-time, event-driven paper backtest. Does not replace BacktestEngine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from atlas.backtesting.costs import ExecutionModel, commission, slipped_price
from atlas.backtesting.journal import JsonlJournal
from atlas.backtesting.strategy import NextOpenBuyStrategy, Strategy
from atlas.backtesting.view import MarketView, build_view
from atlas.config import AtlasSettings
from atlas.contracts import EventClock, MarketBarRecord
from atlas.domain import PriceMark, Signal, SignalSide
from atlas.ingest.local import LocalFixtureSource
from atlas.paper_trading.broker import PaperBroker, PaperFill
from atlas.risk.manager import RiskDecision, RiskManager
from atlas.safety import SafetyError


@dataclass(frozen=True, slots=True)
class PitBacktestResult:
    run_id: str
    starting_cash: float
    ending_cash: float
    ending_equity: float
    fills: int
    rejected: int
    cancelled: int
    equity_curve: tuple[float, ...]
    journal: JsonlJournal


class PitBacktestEngine:
    """Replay local fixtures: decide on available close, fill at the next bar open."""

    def __init__(
        self,
        settings: AtlasSettings | None = None,
        risk: RiskManager | None = None,
        execution: ExecutionModel | None = None,
    ) -> None:
        self.settings = settings or AtlasSettings.load()
        self.risk = risk or RiskManager()
        self.execution = execution or ExecutionModel()

    def run(
        self,
        source: LocalFixtureSource,
        strategy: Strategy | None = None,
        *,
        run_id: str | None = None,
        journal_path: Path | None = None,
    ) -> PitBacktestResult:
        strategy = strategy or NextOpenBuyStrategy()
        run_id = run_id or uuid4().hex
        journal = JsonlJournal(journal_path)
        clock = EventClock(source.stream())
        bars = _ordered_bars(source.all_bars())
        if not bars:
            raise ValueError("bars are required.")
        risk = RiskManager(limits=self.risk.limits, id_factory=_stable_ids(run_id))
        broker = PaperBroker.from_settings(self.settings, risk=risk)
        pending: tuple[RiskDecision, Signal, str] | None = None
        rejected = 0
        cancelled = 0
        equity_curve: list[float] = []

        for index, bar in enumerate(bars):
            if pending is not None:
                filled = self._execute_pending(broker, pending, bar, run_id, journal)
                if not filled:
                    rejected += 1
                pending = None

            self._mark_close(broker, bar)
            view = build_view(
                source, clock.visible(bar.time.available_time), bar.time.available_time
            )
            self._assert_no_lookahead(view, bar.time.available_time)
            journal.append(
                {
                    "type": "view",
                    "run_id": run_id,
                    "as_of": bar.time.available_time.isoformat(),
                    "visible_event_ids": [event.event_id for event in view.events],
                }
            )
            signal = strategy.on_as_of(view)
            if signal is None or signal.side is SignalSide.HOLD:
                equity_curve.append(broker.marked_equity())
                continue
            decision = broker.risk.evaluate(signal, broker.snapshot())
            journal.append(
                {
                    "type": "decision",
                    "run_id": run_id,
                    "as_of": bar.time.available_time.isoformat(),
                    "event_id": f"bar:{bar.symbol}:{bar.time.event_time.isoformat()}",
                    "decision_id": decision.decision_id,
                    "approved": decision.approved,
                    "symbol": signal.symbol,
                    "side": signal.side.value,
                    "quantity": signal.quantity,
                    "reference_price": signal.reference_price,
                    "reasons": list(decision.reasons),
                }
            )
            if not decision.approved:
                rejected += 1
                equity_curve.append(broker.marked_equity())
                continue
            if index == len(bars) - 1:
                cancelled += 1
                journal.append(
                    {
                        "type": "cancel",
                        "run_id": run_id,
                        "reason": "no_next_bar",
                        "decision_id": decision.decision_id,
                    }
                )
            else:
                pending = (decision, signal, f"bar:{bar.symbol}:{bar.time.event_time.isoformat()}")
            equity_curve.append(broker.marked_equity())

        journal.persist()
        return PitBacktestResult(
            run_id=run_id,
            starting_cash=self.settings.starting_cash,
            ending_cash=broker.cash,
            ending_equity=broker.marked_equity(),
            fills=len(broker.fills),
            rejected=rejected,
            cancelled=cancelled,
            equity_curve=tuple(equity_curve),
            journal=journal,
        )

    def _execute_pending(
        self,
        broker: PaperBroker,
        pending: tuple[RiskDecision, Signal, str],
        bar: MarketBarRecord,
        run_id: str,
        journal: JsonlJournal,
    ) -> bool:
        decision, signal, origin_event_id = pending
        fill_price = slipped_price(bar.open, signal.side, self.execution)
        fee = commission(signal, fill_price, self.execution)
        try:
            fill = broker.submit(decision, fill_price=fill_price)
            if fee:
                broker.apply_cash_charge(fee, "simulated commission")
        except SafetyError as exc:
            journal.append(
                {
                    "type": "reject",
                    "run_id": run_id,
                    "origin_event_id": origin_event_id,
                    "execute_event_id": f"bar:{bar.symbol}:{bar.time.event_time.isoformat()}",
                    "open": bar.open,
                    "intended_fill_price": fill_price,
                    "error": str(exc),
                }
            )
            return False
        self._log_fill(journal, run_id, origin_event_id, bar, fill, fill_price, fee, broker)
        return True

    def _log_fill(
        self,
        journal: JsonlJournal,
        run_id: str,
        origin_event_id: str,
        bar: MarketBarRecord,
        fill: PaperFill,
        fill_price: float,
        fee: float,
        broker: PaperBroker,
    ) -> None:
        journal.append(
            {
                "type": "fill",
                "run_id": run_id,
                "origin_event_id": origin_event_id,
                "execute_event_id": f"bar:{bar.symbol}:{bar.time.event_time.isoformat()}",
                "fill_id": fill.fill_id,
                "risk_decision_id": fill.risk_decision_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "open": bar.open,
                "fill_price": fill_price,
                "reference_price": fill.reference_price,
                "commission": fee,
                "cash_after": broker.cash,
                "equity_after_open": None,
                "simulated": True,
            }
        )

    def _mark_close(self, broker: PaperBroker, bar: MarketBarRecord) -> None:
        broker.update_marks(
            {
                bar.symbol: PriceMark(
                    symbol=bar.symbol,
                    price=bar.close,
                    as_of=bar.time.event_time,
                    provenance=(
                        "simulated"
                        if bar.provenance.source_kind.value == "simulated"
                        else "caller_supplied"
                    ),
                )
            }
        )

    def _assert_no_lookahead(self, view: MarketView, as_of: datetime) -> None:
        for event in view.events:
            if event.time.available_time > as_of:
                raise SafetyError("strategy view leaked an event after as_of")


def _stable_ids(run_id: str) -> Callable[[], str]:
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"{run_id}-{counter:06d}"

    return next_id


def _ordered_bars(bars: tuple[MarketBarRecord, ...]) -> list[MarketBarRecord]:
    return sorted(
        bars,
        key=lambda bar: (bar.time.available_time, bar.time.event_time, bar.symbol),
    )
