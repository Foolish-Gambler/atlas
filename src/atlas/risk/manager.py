"""Deterministic risk checks required before any paper fill."""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.domain import PortfolioSnapshot, Signal, SignalSide


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_qty: int = 100
    max_order_qty: int = 50
    max_notional: float = 10_000.0
    min_confidence: float = 0.55
    blocked_symbols: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    signal: Signal

    @property
    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "rejected: " + "; ".join(self.reasons)


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(self, signal: Signal, portfolio: PortfolioSnapshot) -> RiskDecision:
        reasons: list[str] = []
        symbol = signal.symbol.upper()
        if symbol in {item.upper() for item in self.limits.blocked_symbols}:
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
        current_qty = portfolio.positions.get(signal.symbol, 0)
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
        return RiskDecision(approved=not reasons, reasons=tuple(reasons), signal=signal)
