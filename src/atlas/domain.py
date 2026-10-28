"""Shared domain types used across ATLAS modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


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


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash: float
    positions: dict[str, int]
    equity: float
