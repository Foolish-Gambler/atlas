"""Simulated execution costs for paper backtests."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain import Signal, SignalSide
from atlas.validation import require_non_negative_finite, require_positive_finite


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    slippage_bps: float = 0.0
    commission_per_share: float = 0.0
    commission_bps: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "slippage_bps", require_non_negative_finite(self.slippage_bps, "slippage_bps")
        )
        object.__setattr__(
            self,
            "commission_per_share",
            require_non_negative_finite(self.commission_per_share, "commission_per_share"),
        )
        object.__setattr__(
            self,
            "commission_bps",
            require_non_negative_finite(self.commission_bps, "commission_bps"),
        )
        if self.slippage_bps > 10_000:
            raise ValueError("slippage_bps cannot exceed 10000.")


def slipped_price(open_price: float, side: SignalSide, model: ExecutionModel) -> float:
    base = require_positive_finite(open_price, "open_price")
    bump = model.slippage_bps / 10_000.0
    if side is SignalSide.BUY:
        return require_positive_finite(base * (1.0 + bump), "fill_price")
    if side is SignalSide.SELL:
        return require_positive_finite(base * (1.0 - bump), "fill_price")
    raise ValueError("hold signals have no fill price.")


def commission(signal: Signal, fill_price: float, model: ExecutionModel) -> float:
    notional = signal.quantity * fill_price
    return signal.quantity * model.commission_per_share + notional * (
        model.commission_bps / 10_000.0
    )
