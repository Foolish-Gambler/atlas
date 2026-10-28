"""In-memory paper broker. Refuses live connectivity."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from atlas.config import AtlasSettings
from atlas.domain import (
    MissingMarkError,
    PortfolioSnapshot,
    PriceMark,
    Signal,
    SignalSide,
    StaleMarkError,
    TradingMode,
)
from atlas.risk.manager import RiskDecision, RiskManager
from atlas.safety import SafetyError, assert_no_live_broker, assert_paper_only
from atlas.validation import (
    normalize_symbol,
    require_int,
    require_non_negative_finite,
    require_positive_finite,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PaperFill:
    symbol: str
    side: SignalSide
    quantity: int
    price: float
    simulated: bool = True
    fill_id: int = 0
    cash_after: float = 0.0
    risk_decision_id: str = ""
    reference_price: float = 0.0
    commission: float = 0.0


@dataclass
class PaperBroker:
    settings: AtlasSettings
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)
    risk: RiskManager = field(default_factory=RiskManager)
    max_mark_age: timedelta | None = None
    _marks: dict[str, PriceMark] = field(default_factory=dict, repr=False)
    _next_fill_id: int = field(default=1, repr=False)
    _clock: Callable[[], datetime] = field(default=_utc_now)

    def __post_init__(self) -> None:
        assert_paper_only(self.settings)
        assert_no_live_broker(self.settings.live_broker_connected)
        if self.settings.trading_mode is not TradingMode.PAPER:
            raise SafetyError("PaperBroker requires paper trading mode.")
        self.cash = require_non_negative_finite(self.cash, "cash")
        normalized: dict[str, int] = {}
        for symbol, quantity in self.positions.items():
            key = normalize_symbol(symbol)
            qty = require_int(quantity, "position quantity")
            if qty < 0:
                raise SafetyError("position quantity cannot be negative.")
            if qty:
                normalized[key] = normalized.get(key, 0) + qty
        self.positions = normalized

    @classmethod
    def from_settings(
        cls,
        settings: AtlasSettings | None = None,
        risk: RiskManager | None = None,
    ) -> PaperBroker:
        resolved = settings or AtlasSettings.load()
        return cls(settings=resolved, cash=resolved.starting_cash, risk=risk or RiskManager())

    @property
    def mode(self) -> TradingMode:
        return TradingMode.PAPER

    def update_marks(self, marks: Mapping[str, PriceMark] | Mapping[str, float]) -> None:
        now = self._clock()
        for symbol, mark in marks.items():
            resolved = (
                mark
                if isinstance(mark, PriceMark)
                else PriceMark(symbol=symbol, price=float(mark), as_of=now)
            )
            self._marks[resolved.symbol] = resolved

    def snapshot(self, now: datetime | None = None) -> PortfolioSnapshot:
        as_of = now or self._clock()
        missing: list[str] = []
        stale: list[str] = []
        market_value = 0.0
        for symbol, quantity in self.positions.items():
            mark = self._marks.get(symbol)
            if mark is None:
                missing.append(symbol)
                continue
            if self._is_stale(mark, as_of):
                stale.append(symbol)
                continue
            market_value += quantity * mark.price
        incomplete = bool(missing or stale)
        equity = None if incomplete else self.cash + market_value
        return PortfolioSnapshot(
            cash=self.cash,
            positions=dict(self.positions),
            equity=equity,
            missing_marks=tuple(sorted(missing)),
            stale_marks=tuple(sorted(stale)),
        )

    def marked_equity(self, now: datetime | None = None) -> float:
        snap = self.snapshot(now=now)
        if snap.missing_marks:
            raise MissingMarkError(
                "Missing mark-to-market prices for: " + ", ".join(snap.missing_marks)
            )
        if snap.stale_marks:
            raise StaleMarkError("Stale mark-to-market prices for: " + ", ".join(snap.stale_marks))
        if snap.equity is None:
            raise MissingMarkError("Marked equity is unavailable.")
        return snap.equity

    def apply_cash_charge(self, amount: float, reason: str) -> None:
        """Deduct simulated fees after a fill. Never used for live brokerage."""

        assert_paper_only(self.settings)
        charge = require_non_negative_finite(amount, "amount")
        if charge == 0:
            return
        if charge > self.cash:
            raise SafetyError(f"insufficient cash for {reason}")
        self.cash -= charge

    def simulated_fill_price(self, signal: Signal, fill_price: float | None = None) -> float:
        """Return the broker-controlled simulated fill.

        `Signal.reference_price` is only a fallback when no current mark exists.
        """

        if fill_price is not None:
            return require_positive_finite(fill_price, "fill_price")
        mark = self._marks.get(signal.symbol)
        now = self._clock()
        if mark is not None and not self._is_stale(mark, now):
            return mark.price
        return signal.reference_price

    def submit(
        self,
        decision: RiskDecision,
        *,
        fill_price: float | None = None,
        commission: float = 0.0,
    ) -> PaperFill:
        assert_paper_only(self.settings)
        issued = self._require_issued(decision)
        if self.risk.is_consumed(issued.decision_id):
            raise SafetyError("RiskDecision has already been submitted.")
        current = self.snapshot()
        live = self.risk.evaluate(issued.signal, current)
        if not live.approved:
            raise SafetyError(f"Signal rejected by risk: {live.summary}")
        price = self.simulated_fill_price(issued.signal, fill_price)
        fill = self._apply(issued.signal, issued.decision_id, price, commission)
        self.risk.consume(issued.decision_id)
        return fill

    def _require_issued(self, decision: RiskDecision) -> RiskDecision:
        if not decision.decision_id:
            raise SafetyError("RiskDecision is missing a stable identifier.")
        issued = self.risk.lookup(decision.decision_id)
        if issued is None:
            raise SafetyError("unknown or forged RiskDecision")
        if issued.signal != decision.signal:
            raise SafetyError("RiskDecision signal does not match the issued decision.")
        if not issued.approved:
            raise SafetyError("previously rejected RiskDecision cannot be executed")
        return issued

    def _apply(
        self,
        signal: Signal,
        decision_id: str,
        fill_price: float,
        commission: float = 0.0,
    ) -> PaperFill:
        fill_price = require_positive_finite(fill_price, "fill_price")
        fee = require_non_negative_finite(commission, "commission")
        notional = signal.quantity * fill_price
        if notional > self.risk.limits.max_notional:
            raise SafetyError("fill notional exceeds max_notional")
        if signal.side is SignalSide.BUY:
            total = notional + fee
            if total > self.cash:
                raise SafetyError("insufficient cash for buy including commission")
            new_cash = self.cash - total
            new_qty = self.positions.get(signal.symbol, 0) + signal.quantity
            if new_cash < 0 or new_qty < 0:
                raise SafetyError("order would make cash or holdings negative.")
            self.cash = new_cash
            self.positions[signal.symbol] = new_qty
        elif signal.side is SignalSide.SELL:
            current_qty = self.positions.get(signal.symbol, 0)
            if signal.quantity > current_qty:
                raise SafetyError("cannot sell more than the current paper position")
            new_cash = self.cash + notional - fee
            remaining = current_qty - signal.quantity
            if new_cash < 0 or remaining < 0:
                raise SafetyError("order would make cash or holdings negative.")
            self.cash = new_cash
            if remaining:
                self.positions[signal.symbol] = remaining
            else:
                self.positions.pop(signal.symbol, None)
        else:
            raise SafetyError("Hold signals cannot be submitted.")
        self.update_marks(
            {
                signal.symbol: PriceMark(
                    symbol=signal.symbol,
                    price=fill_price,
                    as_of=self._clock(),
                    provenance="simulated",
                )
            }
        )
        fill = PaperFill(
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=fill_price,
            simulated=True,
            fill_id=self._next_fill_id,
            cash_after=self.cash,
            risk_decision_id=decision_id,
            reference_price=signal.reference_price,
            commission=fee,
        )
        self._next_fill_id += 1
        self.fills.append(fill)
        return fill

    def _is_stale(self, mark: PriceMark, now: datetime) -> bool:
        if self.max_mark_age is None:
            return False
        return now - mark.as_of > self.max_mark_age
