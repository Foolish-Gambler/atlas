from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas.config import AtlasSettings
from atlas.domain import (
    MissingMarkError,
    PortfolioSnapshot,
    PriceMark,
    Signal,
    SignalSide,
    StaleMarkError,
)
from atlas.paper_trading import PaperBroker
from atlas.risk import RiskDecision, RiskLimits, RiskManager
from atlas.safety import SafetyError
from atlas.validation import InvalidInputError


def _buy(
    symbol: str = "SIM-A",
    quantity: int = 2,
    price: float = 100.0,
    confidence: float = 0.9,
) -> Signal:
    return Signal(
        symbol=symbol,
        side=SignalSide.BUY,
        quantity=quantity,
        reference_price=price,
        confidence=confidence,
        rationale="test",
        source="test",
    )


def _sell(symbol: str = "SIM-A", quantity: int = 1, price: float = 100.0) -> Signal:
    return Signal(
        symbol=symbol,
        side=SignalSide.SELL,
        quantity=quantity,
        reference_price=price,
        confidence=0.9,
        rationale="test",
        source="test",
    )


def test_forged_approval_is_revalidated(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    oversized = _buy(quantity=50, price=1_000.0)
    forged = RiskDecision(approved=True, reasons=(), signal=oversized, decision_id="forged")
    with pytest.raises(SafetyError, match="rejected by risk"):
        broker.submit(forged)
    assert broker.fills == []
    assert broker.cash == settings.starting_cash


def test_stale_decision_uses_current_portfolio(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    first = RiskManager().evaluate(_buy(quantity=40, price=200.0), broker.snapshot())
    assert first.approved is True
    spent = RiskManager().evaluate(_buy(quantity=20, price=200.0), broker.snapshot())
    broker.submit(spent)
    with pytest.raises(SafetyError, match="insufficient cash"):
        broker.submit(first)


def test_duplicate_submission_is_rejected(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    decision = RiskManager().evaluate(_buy(), broker.snapshot())
    broker.submit(decision)
    with pytest.raises(SafetyError, match="already been submitted"):
        broker.submit(decision)
    assert len(broker.fills) == 1


def test_insufficient_cash_rejected(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    decision = RiskManager().evaluate(_buy(quantity=50, price=1_000.0), broker.snapshot())
    assert decision.approved is False
    with pytest.raises(SafetyError, match="insufficient cash"):
        broker.submit(decision)
    assert broker.cash == settings.starting_cash
    assert broker.positions == {}


def test_oversell_rejected(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    buy = RiskManager().evaluate(_buy(quantity=2), broker.snapshot())
    broker.submit(buy)
    sell = RiskDecision(
        approved=True,
        reasons=(),
        signal=_sell(quantity=5),
        decision_id="forged-sell",
    )
    with pytest.raises(SafetyError, match="cannot sell more"):
        broker.submit(sell)
    assert broker.positions["SIM-A"] == 2


def test_invalid_numeric_inputs_rejected() -> None:
    with pytest.raises(InvalidInputError):
        _buy(price=float("nan"))
    with pytest.raises(InvalidInputError):
        _buy(price=float("inf"))
    with pytest.raises(InvalidInputError):
        Signal(
            symbol="SIM-A",
            side=SignalSide.BUY,
            quantity=1,
            reference_price=100.0,
            confidence=1.1,
            rationale="test",
            source="test",
        )
    with pytest.raises(InvalidInputError):
        Signal(
            symbol="SIM-A",
            side=SignalSide.BUY,
            quantity=1,
            reference_price=100.0,
            confidence=float("nan"),
            rationale="test",
            source="test",
        )
    with pytest.raises(ValueError):
        Signal(
            symbol="SIM-A",
            side=SignalSide.BUY,
            quantity=-1,
            reference_price=100.0,
            confidence=0.9,
            rationale="test",
            source="test",
        )
    with pytest.raises(InvalidInputError):
        Signal(
            symbol="SIM-A",
            side=SignalSide.BUY,
            quantity=True,  # type: ignore[arg-type]
            reference_price=100.0,
            confidence=0.9,
            rationale="test",
            source="test",
        )


def test_symbol_casing_is_normalized(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    buy = RiskManager().evaluate(_buy(symbol="sim-a", quantity=2), broker.snapshot())
    broker.submit(buy)
    sell = RiskManager().evaluate(_sell(symbol="Sim-A", quantity=2), broker.snapshot())
    fill = broker.submit(sell)
    assert fill.symbol == "SIM-A"
    assert broker.positions == {}
    assert "sim-a" not in broker.positions


def test_equity_includes_mark_to_market(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    buy = RiskManager().evaluate(_buy(quantity=2, price=100.0), broker.snapshot())
    broker.submit(buy)
    broker.update_marks(
        {
            "SIM-A": PriceMark(
                symbol="SIM-A",
                price=110.0,
                as_of=datetime(2024, 1, 3, tzinfo=UTC),
                provenance="simulated",
            )
        }
    )
    snap = broker.snapshot()
    assert snap.marked is True
    assert snap.equity == pytest.approx(settings.starting_cash - 200.0 + 220.0)
    assert broker.marked_equity() == snap.equity


def test_missing_marks_are_explicit(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    broker.submit(RiskManager().evaluate(_buy(), broker.snapshot()))
    broker._marks.clear()
    snap = broker.snapshot()
    assert snap.missing_marks == ("SIM-A",)
    assert snap.marked is False
    assert snap.equity == broker.cash
    with pytest.raises(MissingMarkError):
        broker.marked_equity()


def test_stale_marks_are_explicit(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    broker.max_mark_age = timedelta(seconds=1)
    broker.submit(RiskManager().evaluate(_buy(), broker.snapshot()))
    broker.update_marks(
        {
            "SIM-A": PriceMark(
                symbol="SIM-A",
                price=100.0,
                as_of=datetime(2020, 1, 1, tzinfo=UTC),
                provenance="simulated",
            )
        }
    )
    snap = broker.snapshot(now=datetime(2024, 1, 1, tzinfo=UTC))
    assert snap.stale_marks == ("SIM-A",)
    with pytest.raises(StaleMarkError):
        broker.marked_equity(now=datetime(2024, 1, 1, tzinfo=UTC))


def test_cash_and_holdings_stay_non_negative(settings: AtlasSettings) -> None:
    broker = PaperBroker.from_settings(settings)
    broker.submit(RiskManager().evaluate(_buy(quantity=2, price=100.0), broker.snapshot()))
    broker.cash = 10.0
    with pytest.raises(SafetyError):
        broker.submit(
            RiskDecision(
                approved=True,
                reasons=(),
                signal=_buy(quantity=2, price=100.0),
                decision_id="tamper",
            )
        )
    assert broker.cash == 10.0
    assert broker.positions["SIM-A"] == 2


def test_invalid_risk_limits_rejected() -> None:
    with pytest.raises(InvalidInputError):
        RiskLimits(max_notional=float("nan"))
    with pytest.raises(InvalidInputError):
        RiskLimits(min_confidence=1.5)
    with pytest.raises(InvalidInputError):
        RiskLimits(max_order_qty=0)
    with pytest.raises(InvalidInputError):
        RiskLimits(max_position_qty=-1)


def test_portfolio_snapshot_rejects_negative_cash() -> None:
    with pytest.raises(InvalidInputError):
        PortfolioSnapshot(cash=-1.0, positions={}, equity=-1.0)
