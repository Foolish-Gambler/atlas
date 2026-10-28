"""Point-in-time, event-driven paper backtest. The research path; not BacktestEngine."""

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
from atlas.contracts import (
    EventClock,
    MarketBarRecord,
    PitReplaySource,
    StreamEvent,
    StreamEventType,
    event_sort_key,
)
from atlas.domain import PriceMark, Signal, SignalSide
from atlas.paper_trading.broker import PaperBroker, PaperFill
from atlas.risk.manager import RiskDecision, RiskManager
from atlas.safety import SafetyError


class SingleSymbolError(ValueError):
    """M1 pit replay supports one tradable symbol."""


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
    ending_positions: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Pending:
    decision: RiskDecision
    signal: Signal
    origin_event_id: str
    decision_time: datetime
    symbol: str


class PitBacktestEngine:
    """Decide on bar-close publication; fill at a later same-symbol session open."""

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
        source: PitReplaySource,
        strategy: Strategy | None = None,
        *,
        run_id: str | None = None,
        journal_path: Path | None = None,
    ) -> PitBacktestResult:
        strategy = strategy or NextOpenBuyStrategy()
        run_id = run_id or uuid4().hex
        journal = JsonlJournal(journal_path)
        bars = source.all_bars()
        if not bars:
            raise ValueError("bars are required.")
        symbols = {bar.symbol for bar in bars}
        if len(symbols) != 1:
            raise SingleSymbolError(
                "M1 pit backtests support a single symbol; "
                f"got {sorted(symbols)}. Multi-asset scheduling is not implemented."
            )
        clock = EventClock(source.stream())
        _assert_monotonic(clock.events)
        risk = RiskManager(limits=self.risk.limits, id_factory=_stable_ids(run_id))
        broker = PaperBroker.from_settings(self.settings, risk=risk)
        pending: _Pending | None = None
        rejected = 0
        cancelled = 0
        equity_curve: list[float] = []
        last_close_time: dict[str, datetime] = {}

        for event in clock.events:
            if event.event_type is StreamEventType.BAR_OPEN:
                bar = source.bar_by_ref(event.ref_id)
                if pending is not None and _is_eligible_open(pending, bar):
                    filled = self._execute_pending(broker, pending, bar, run_id, journal)
                    if not filled:
                        rejected += 1
                    pending = None
                elif pending is not None and bar.symbol != pending.symbol:
                    raise SafetyError("pending order cannot execute on a different symbol's bar")
                continue

            if event.event_type is not StreamEventType.BAR_CLOSE:
                continue

            bar = source.bar_by_ref(event.ref_id)
            self._mark_close(broker, bar, last_close_time)
            as_of = bar.available_time
            view = build_view(source, clock.visible(as_of), as_of)
            self._assert_no_lookahead(view, as_of)
            journal.append(
                {
                    "type": "view",
                    "run_id": run_id,
                    "as_of": as_of.isoformat(),
                    "event_id": event.event_id,
                    "visible_event_ids": [item.event_id for item in view.events],
                }
            )
            if pending is not None:
                equity_curve.append(broker.marked_equity())
                continue
            signal = strategy.on_as_of(view)
            if signal is None or signal.side is SignalSide.HOLD:
                equity_curve.append(broker.marked_equity())
                continue
            if signal.symbol != bar.symbol:
                raise SafetyError("M1 strategy signal must match the fixture symbol")
            decision = broker.risk.evaluate(signal, broker.snapshot())
            journal.append(
                {
                    "type": "decision",
                    "run_id": run_id,
                    "as_of": as_of.isoformat(),
                    "event_id": bar.close_event_id,
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
            pending = _Pending(
                decision=decision,
                signal=signal,
                origin_event_id=bar.close_event_id,
                decision_time=as_of,
                symbol=signal.symbol,
            )
            equity_curve.append(broker.marked_equity())

        if pending is not None:
            cancelled += 1
            journal.append(
                {
                    "type": "cancel",
                    "run_id": run_id,
                    "reason": "no_later_same_symbol_open",
                    "decision_id": pending.decision.decision_id,
                    "decision_time": pending.decision_time.isoformat(),
                }
            )

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
            ending_positions=dict(broker.positions),
        )

    def _execute_pending(
        self,
        broker: PaperBroker,
        pending: _Pending,
        bar: MarketBarRecord,
        run_id: str,
        journal: JsonlJournal,
    ) -> bool:
        if bar.symbol != pending.symbol:
            raise SafetyError("pending order cannot execute on a different symbol's bar")
        if bar.open_time <= pending.decision_time:
            raise SafetyError("execution timestamp must be after the decision timestamp")
        fill_price = slipped_price(bar.open, pending.signal.side, self.execution)
        fee = commission(pending.signal, fill_price, self.execution)
        cash_before = broker.cash
        positions_before = dict(broker.positions)
        fills_before = len(broker.fills)
        try:
            fill = broker.submit(pending.decision, fill_price=fill_price, commission=fee)
        except SafetyError as exc:
            if (
                broker.cash != cash_before
                or dict(broker.positions) != positions_before
                or len(broker.fills) != fills_before
                or broker.risk.is_consumed(pending.decision.decision_id)
            ):
                raise SafetyError("rejected fill mutated paper state") from exc
            journal.append(
                {
                    "type": "reject",
                    "run_id": run_id,
                    "origin_event_id": pending.origin_event_id,
                    "execute_event_id": bar.open_event_id,
                    "decision_time": pending.decision_time.isoformat(),
                    "execution_time": bar.open_time.isoformat(),
                    "open": bar.open,
                    "intended_fill_price": fill_price,
                    "commission": fee,
                    "error": str(exc),
                }
            )
            return False
        self._log_fill(journal, run_id, pending, bar, fill, fill_price, fee, broker)
        return True

    def _log_fill(
        self,
        journal: JsonlJournal,
        run_id: str,
        pending: _Pending,
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
                "origin_event_id": pending.origin_event_id,
                "execute_event_id": bar.open_event_id,
                "decision_time": pending.decision_time.isoformat(),
                "execution_time": bar.open_time.isoformat(),
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
                "simulated": True,
            }
        )

    def _mark_close(
        self,
        broker: PaperBroker,
        bar: MarketBarRecord,
        last_close_time: dict[str, datetime],
    ) -> None:
        previous = last_close_time.get(bar.symbol)
        if previous is not None and bar.close_time <= previous:
            return
        last_close_time[bar.symbol] = bar.close_time
        broker.update_marks(
            {
                bar.symbol: PriceMark(
                    symbol=bar.symbol,
                    price=bar.close,
                    as_of=bar.close_time,
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
        for bar in view.bars:
            if bar.available_time > as_of or bar.close_time > as_of:
                raise SafetyError("strategy view leaked an incomplete or future bar")
        for filing in view.filings:
            if filing.time.available_time > as_of:
                raise SafetyError("strategy view leaked a future filing")
        for artifact in view.research:
            if artifact.time.available_time > as_of:
                raise SafetyError("strategy view leaked future research")
        for prediction in view.predictions:
            if prediction.time.available_time > as_of:
                raise SafetyError("strategy view leaked a future prediction")


def _is_eligible_open(pending: _Pending, bar: MarketBarRecord) -> bool:
    return bar.symbol == pending.symbol and bar.open_time > pending.decision_time


def _assert_monotonic(events: tuple[StreamEvent, ...]) -> None:
    previous = None
    for event in events:
        key = event_sort_key(event)
        if previous is not None and key < previous:
            raise SafetyError("event stream is not monotonically ordered")
        previous = key


def _stable_ids(run_id: str) -> Callable[[], str]:
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"{run_id}-{counter:06d}"

    return next_id
