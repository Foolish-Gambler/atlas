"""Process configuration. Secrets are never stored here."""

from __future__ import annotations

import os
from dataclasses import dataclass

from atlas.domain import TradingMode
from atlas.validation import InvalidInputError, require_positive_finite

_ALLOWED_MODES = frozenset({TradingMode.PAPER})


@dataclass(frozen=True, slots=True)
class AtlasSettings:
    """Runtime settings loaded from environment with safe defaults.

    `ATLAS_TRADING_MODE` is accepted only when set to `paper`. Any other
    value is rejected at load time so live mode cannot be enabled by accident.
    """

    trading_mode: TradingMode = TradingMode.PAPER
    starting_cash: float = 100_000.0
    live_broker_connected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "starting_cash", require_positive_finite(self.starting_cash, "starting_cash")
        )
        if self.live_broker_connected:
            raise InvalidInputError("live_broker_connected must remain False.")

    @classmethod
    def load(cls) -> AtlasSettings:
        raw_mode = os.environ.get("ATLAS_TRADING_MODE", TradingMode.PAPER.value)
        try:
            mode = TradingMode(raw_mode.strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"Invalid ATLAS_TRADING_MODE={raw_mode!r}. Only 'paper' is allowed."
            ) from exc
        if mode not in _ALLOWED_MODES:
            raise ValueError(
                "ATLAS_TRADING_MODE cannot enable live trading. Only 'paper' is allowed."
            )
        raw_cash = os.environ.get("ATLAS_STARTING_CASH", "100000")
        try:
            starting_cash = float(raw_cash)
        except (TypeError, ValueError) as exc:
            raise ValueError("ATLAS_STARTING_CASH must be a finite positive number.") from exc
        return cls(
            trading_mode=mode,
            starting_cash=starting_cash,
            live_broker_connected=False,
        )
