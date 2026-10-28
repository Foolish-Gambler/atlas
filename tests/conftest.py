from __future__ import annotations

import pytest

from atlas.config import AtlasSettings
from atlas.domain import TradingMode
from atlas.risk.manager import RiskManager


@pytest.fixture
def settings() -> AtlasSettings:
    return AtlasSettings(
        trading_mode=TradingMode.PAPER,
        starting_cash=10_000.0,
        live_broker_connected=False,
    )


@pytest.fixture
def risk_manager() -> RiskManager:
    return RiskManager()
