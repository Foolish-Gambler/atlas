from __future__ import annotations

import pytest

from atlas.market_data import SimulatedMarketDataProvider


def test_simulated_quotes_are_labeled() -> None:
    provider = SimulatedMarketDataProvider()
    quote = provider.get_quote("sim-a")
    assert quote.symbol == "SIM-A"
    assert quote.provenance == "simulated"
    assert "Not a real market quote" in quote.disclaimer
    assert quote.price > 0


def test_history_is_deterministic() -> None:
    provider = SimulatedMarketDataProvider(base_price=50)
    first = provider.get_history("SIM-A", 3)
    second = provider.get_history("SIM-A", 3)
    assert first == second
    assert all(bar.provenance == "simulated" for bar in first)


def test_invalid_symbol_rejected() -> None:
    with pytest.raises(ValueError):
        SimulatedMarketDataProvider().get_quote("  ")
