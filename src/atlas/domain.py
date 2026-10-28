"""Shared domain types used across ATLAS modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from atlas.validation import (
    normalize_symbol,
    require_finite,
    require_int,
    require_non_negative_finite,
    require_positive_finite,
    require_unit_interval,
)


class TradingMode(StrEnum):
    """Allowed execution modes.

    LIVE is defined only so misconfiguration can be detected and rejected.
    """

    PAPER = "paper"
    LIVE = "live"


class SignalSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


DataProvenance = Literal["simulated", "caller_supplied"]


class MissingMarkError(ValueError):
    """A position has no mark-to-market price."""


class StaleMarkError(ValueError):
    """A stored mark is older than the allowed age."""


@dataclass(frozen=True, slots=True)
class PriceMark:
    """Simulated or caller-supplied price used for portfolio valuation."""

    symbol: str
    price: float
    as_of: datetime
    provenance: DataProvenance = "simulated"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "price", require_positive_finite(self.price, "price"))


@dataclass(frozen=True, slots=True)
class Signal:
    """A candidate trade. Never executed until risk approves it."""

    symbol: str
    side: SignalSide
    quantity: int
    reference_price: float
    confidence: float
    rationale: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not isinstance(self.side, SignalSide):
            raise ValueError("side must be a SignalSide.")
        quantity = require_int(self.quantity, "quantity")
        if self.side is SignalSide.HOLD:
            if quantity != 0:
                raise ValueError("hold quantity must be 0.")
        elif quantity <= 0:
            raise ValueError("quantity must be a positive integer for buy/sell.")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self,
            "reference_price",
            require_positive_finite(self.reference_price, "reference_price"),
        )
        object.__setattr__(self, "confidence", require_unit_interval(self.confidence, "confidence"))


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash: float
    positions: dict[str, int]
    equity: float
    missing_marks: tuple[str, ...] = ()
    stale_marks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cash", require_non_negative_finite(self.cash, "cash"))
        object.__setattr__(self, "equity", require_finite(self.equity, "equity"))
        normalized: dict[str, int] = {}
        for symbol, quantity in self.positions.items():
            key = normalize_symbol(symbol)
            qty = require_int(quantity, "position quantity")
            if qty < 0:
                raise ValueError("position quantity cannot be negative.")
            if qty == 0:
                continue
            normalized[key] = normalized.get(key, 0) + qty
        object.__setattr__(self, "positions", normalized)

    @property
    def marked(self) -> bool:
        return not self.missing_marks and not self.stale_marks
