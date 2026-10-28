from __future__ import annotations

import pytest

from atlas.backtesting import BacktestEngine
from atlas.backtesting.engine import LookaheadBiasWarning
from atlas.config import AtlasSettings
from atlas.market_data import SimulatedMarketDataProvider


def test_backtest_uses_simulated_path_and_risk(settings: AtlasSettings) -> None:
    bars = SimulatedMarketDataProvider().get_history("SIM-A", 5)
    with pytest.warns(LookaheadBiasWarning, match="look-ahead"):
        result = BacktestEngine(settings=settings).run(bars, quantity=1)
    assert BacktestEngine.LOOKAHEAD_BIAS is True
    assert result.provenance_ok is True
    assert result.starting_cash == settings.starting_cash
    assert result.fills + result.rejected == 5
    assert result.ending_cash <= result.starting_cash
