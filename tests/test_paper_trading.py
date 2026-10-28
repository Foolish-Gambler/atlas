from __future__ import annotations

import pytest

from atlas.config import AtlasSettings
from atlas.domain import PortfolioSnapshot, Signal, SignalSide, TradingMode
from atlas.paper_trading import PaperBroker
from atlas.risk import RiskDecision, RiskManager
from atlas.safety import SafetyError


def test_paper_broker_fills_only_approved_signals(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    signal = Signal(
        symbol="SIM-A",
        side=SignalSide.BUY,
        quantity=2,
        reference_price=100.0,
        confidence=0.9,
        rationale="test",
        source="test",
    )
    decision = RiskManager().evaluate(signal, broker.snapshot())
    fill = broker.submit(decision)
    assert fill.simulated is True
    assert broker.positions["SIM-A"] == 2
    assert broker.cash == settings.starting_cash - 200.0


def test_paper_broker_rejects_unapproved_signals(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    signal = Signal(
        symbol="SIM-A",
        side=SignalSide.BUY,
        quantity=2,
        reference_price=100.0,
        confidence=0.1,
        rationale="test",
        source="test",
    )
    decision = RiskDecision(approved=False, reasons=("low confidence",), signal=signal)
    with pytest.raises(SafetyError, match="rejected by risk"):
        broker.submit(decision)
    assert broker.fills == []


def test_paper_broker_refuses_live_settings() -> None:
    live = AtlasSettings(
        trading_mode=TradingMode.LIVE,
        starting_cash=1_000.0,
        live_broker_connected=False,
    )
    with pytest.raises(SafetyError):
        PaperBroker.from_settings(live)


def test_cannot_submit_hold(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    signal = Signal(
        symbol="SIM-A",
        side=SignalSide.HOLD,
        quantity=0,
        reference_price=100.0,
        confidence=0.9,
        rationale="test",
        source="test",
    )
    decision = RiskDecision(approved=True, reasons=(), signal=signal)
    with pytest.raises(SafetyError, match="Hold"):
        broker.submit(decision)


def test_snapshot_is_a_copy(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    snap: PortfolioSnapshot = broker.snapshot()
    snap.positions["SIM-A"] = 99
    assert "SIM-A" not in broker.positions
