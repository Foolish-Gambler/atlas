from __future__ import annotations

from atlas.domain import PortfolioSnapshot, Signal, SignalSide
from atlas.forecasting import ForecastEngine, ForecastInputs
from atlas.fundamentals import FundamentalScore
from atlas.news import NewsScore
from atlas.risk import RiskLimits, RiskManager


def _portfolio(
    cash: float = 10_000.0, positions: dict[str, int] | None = None
) -> PortfolioSnapshot:
    return PortfolioSnapshot(cash=cash, positions=positions or {}, equity=cash)


def test_forecast_is_a_signal_not_an_execution() -> None:
    signal = ForecastEngine().propose(
        ForecastInputs(
            symbol="SIM-A",
            last_price=100.0,
            news=NewsScore(
                symbol="SIM-A",
                score=1.0,
                label="positive",
                provenance="caller_supplied",
                headline_count=1,
            ),
            fundamentals=FundamentalScore(
                symbol="SIM-A",
                pe_ratio=10.0,
                pb_ratio=1.0,
                score=1.0,
                provenance="caller_supplied",
            ),
        )
    )
    assert signal.source.startswith("forecasting")
    assert signal.side is SignalSide.BUY
    assert not hasattr(signal, "order_id")


def test_risk_rejects_hold_and_low_confidence() -> None:
    manager = RiskManager(RiskLimits(min_confidence=0.9))
    hold = Signal(
        symbol="SIM-A",
        side=SignalSide.HOLD,
        quantity=0,
        reference_price=100.0,
        confidence=0.4,
        rationale="test",
        source="test",
    )
    decision = manager.evaluate(hold, _portfolio())
    assert decision.approved is False
    assert any("hold" in reason for reason in decision.reasons)


def test_risk_rejects_oversized_notional() -> None:
    manager = RiskManager(RiskLimits(max_notional=500.0, max_order_qty=100))
    signal = Signal(
        symbol="SIM-A",
        side=SignalSide.BUY,
        quantity=10,
        reference_price=100.0,
        confidence=0.9,
        rationale="test",
        source="test",
    )
    decision = manager.evaluate(signal, _portfolio())
    assert decision.approved is False
    assert any("notional" in reason for reason in decision.reasons)


def test_risk_approves_in_limit_buy() -> None:
    manager = RiskManager()
    signal = Signal(
        symbol="SIM-A",
        side=SignalSide.BUY,
        quantity=5,
        reference_price=100.0,
        confidence=0.9,
        rationale="test",
        source="test",
    )
    decision = manager.evaluate(signal, _portfolio())
    assert decision.approved is True
    assert decision.reasons == ()
