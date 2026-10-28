"""Deterministic risk checks required before any paper fill."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from atlas.domain import PortfolioSnapshot, Signal, SignalSide
from atlas.validation import (
    InvalidInputError,
    normalize_symbol,
    require_positive_finite,
    require_unit_interval,
)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_qty: int = 100
    max_order_qty: int = 50
    max_notional: float = 10_000.0
    min_confidence: float = 0.55
    blocked_symbols: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if isinstance(self.max_position_qty, bool) or not isinstance(self.max_position_qty, int):
            raise InvalidInputError("max_position_qty must be an integer.")
        if isinstance(self.max_order_qty, bool) or not isinstance(self.max_order_qty, int):
            raise InvalidInputError("max_order_qty must be an integer.")
        if self.max_position_qty <= 0:
            raise InvalidInputError("max_position_qty must be positive.")
        if self.max_order_qty <= 0:
            raise InvalidInputError("max_order_qty must be positive.")
        if self.max_order_qty > self.max_position_qty:
            raise InvalidInputError("max_order_qty cannot exceed max_position_qty.")
        object.__setattr__(
            self, "max_notional", require_positive_finite(self.max_notional, "max_notional")
        )
        object.__setattr__(
            self, "min_confidence", require_unit_interval(self.min_confidence, "min_confidence")
        )
        blocked = frozenset(normalize_symbol(symbol) for symbol in self.blocked_symbols)
        object.__setattr__(self, "blocked_symbols", blocked)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Outcome of a single `RiskManager.evaluate` call.

    Execution contract:
    - Only decisions issued by a `RiskManager` (non-empty `decision_id`) exist.
    - A rejected decision is permanently non-executable, even if the book later
      would pass the same signal.
    - An approved decision must still pass a current-book check at submit time.
    - `decision_id` is the stable idempotency key; it is not Python `id()`.
    """

    approved: bool
    reasons: tuple[str, ...]
    signal: Signal
    decision_id: str = ""

    @property
    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "rejected: " + "; ".join(self.reasons)

    @property
    def executable(self) -> bool:
        return self.approved and bool(self.decision_id)


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.limits = limits or RiskLimits()
        self._issued: dict[str, RiskDecision] = {}
        self._consumed: set[str] = set()
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    def lookup(self, decision_id: str) -> RiskDecision | None:
        if not decision_id:
            return None
        return self._issued.get(decision_id)

    def is_consumed(self, decision_id: str) -> bool:
        return bool(decision_id) and decision_id in self._consumed

    def consume(self, decision_id: str) -> None:
        if decision_id:
            self._consumed.add(decision_id)

    def evaluate(self, signal: Signal, portfolio: PortfolioSnapshot) -> RiskDecision:
        reasons: list[str] = []
        symbol = signal.symbol
        if symbol in self.limits.blocked_symbols:
            reasons.append(f"{symbol} is blocked")
        if signal.side is SignalSide.HOLD:
            reasons.append("hold signals are not executable")
        if signal.quantity <= 0 and signal.side is not SignalSide.HOLD:
            reasons.append("quantity must be positive for buy/sell")
        if signal.quantity > self.limits.max_order_qty:
            reasons.append(
                f"quantity {signal.quantity} exceeds max_order_qty {self.limits.max_order_qty}"
            )
        if signal.confidence < self.limits.min_confidence:
            reasons.append(
                f"confidence {signal.confidence:.2f} below {self.limits.min_confidence:.2f}"
            )
        if signal.reference_price <= 0:
            reasons.append("reference_price must be positive")
        notional = signal.quantity * signal.reference_price
        if notional > self.limits.max_notional:
            reasons.append(
                f"notional {notional:.2f} exceeds max_notional {self.limits.max_notional:.2f}"
            )
        current_qty = portfolio.positions.get(symbol, 0)
        if signal.side is SignalSide.BUY:
            projected = current_qty + signal.quantity
            if projected > self.limits.max_position_qty:
                reasons.append(
                    f"projected position {projected} exceeds max_position_qty "
                    f"{self.limits.max_position_qty}"
                )
            if notional > portfolio.cash:
                reasons.append("insufficient cash for buy")
        if signal.side is SignalSide.SELL and signal.quantity > current_qty:
            reasons.append("cannot sell more than the current paper position")
        decision = RiskDecision(
            approved=not reasons,
            reasons=tuple(reasons),
            signal=signal,
            decision_id=self._new_id(),
        )
        self._issued[decision.decision_id] = decision
        return decision
