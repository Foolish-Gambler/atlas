"""Process-wide safety gates. Paper trading is the only supported mode."""

from __future__ import annotations

from atlas.config import AtlasSettings
from atlas.domain import TradingMode


class SafetyError(RuntimeError):
    """Raised when a live-trading or unsafe path is requested."""


def assert_paper_only(settings: AtlasSettings | None = None) -> TradingMode:
    """Return PAPER or raise. Call this before any execution path."""

    resolved = settings or AtlasSettings.load()
    if resolved.trading_mode is TradingMode.LIVE:
        raise SafetyError("Live brokerage mode is disabled. ATLAS only simulates paper trades.")
    if resolved.trading_mode is not TradingMode.PAPER:
        raise SafetyError(f"Unsupported trading mode: {resolved.trading_mode!r}")
    return TradingMode.PAPER


def assert_no_live_broker(connected: bool) -> None:
    if connected:
        raise SafetyError("A live brokerage connection is not permitted.")
