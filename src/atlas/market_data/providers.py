"""Market data providers. The default implementation is fully simulated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from atlas.market_data.models import Bar, Quote

SIMULATED_DISCLAIMER = (
    "Simulated fixture data for software tests and paper trading. Not a real market quote."
)


class MarketDataProvider(Protocol):
    name: str

    def get_quote(self, symbol: str) -> Quote: ...

    def get_history(self, symbol: str, periods: int) -> list[Bar]: ...


class SimulatedMarketDataProvider:
    """Deterministic synthetic prices. Never presented as live market data."""

    name = "simulated"

    def __init__(self, base_price: float = 100.0) -> None:
        if base_price <= 0:
            raise ValueError("base_price must be positive.")
        self._base_price = base_price

    def get_quote(self, symbol: str) -> Quote:
        symbol = _require_symbol(symbol)
        return Quote(
            symbol=symbol,
            price=self._price_for(symbol),
            as_of=datetime(2024, 1, 2, tzinfo=UTC),
            provenance="simulated",
            disclaimer=SIMULATED_DISCLAIMER,
        )

    def get_history(self, symbol: str, periods: int) -> list[Bar]:
        symbol = _require_symbol(symbol)
        if periods <= 0:
            raise ValueError("periods must be positive.")
        start = datetime(2024, 1, 2, tzinfo=UTC)
        price = self._price_for(symbol)
        bars: list[Bar] = []
        for index in range(periods):
            close = price * (1.0 + 0.001 * index)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=start + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000 + index,
                    provenance="simulated",
                )
            )
        return bars

    def _price_for(self, symbol: str) -> float:
        offset = sum(ord(char) for char in symbol) % 25
        return round(self._base_price + offset, 2)


def _require_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise ValueError("symbol is required.")
    return cleaned
