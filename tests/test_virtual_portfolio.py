from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from atlas.backtesting.costs import ExecutionModel
from atlas.backtesting.view import MarketView
from atlas.contracts import EventClock
from atlas.domain import Signal
from atlas.ingest.alpaca import AlpacaBarQuery, adapter_for_tests
from atlas.ingest.snapshot import SnapshotStore
from atlas.paper_trading.demo import (
    VIRTUAL_CASH_USD,
    BuyOnceNextOpenStrategy,
    run_virtual_portfolio,
)
from atlas.risk.manager import RiskLimits, RiskManager

FIXTURE = Path(__file__).parent / "fixtures" / "ingest" / "alpaca" / "aapl_1min_pages.json"


@pytest.fixture
def alpaca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def _ingest(tmp_path: Path):
    pages = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"]
    state = {"i": 0}

    def handler(request):
        page = pages[state["i"]]
        state["i"] += 1
        import httpx

        return httpx.Response(200, json=page)

    adapter = adapter_for_tests(handler)
    store = SnapshotStore(tmp_path)
    adapter.ingest(
        store,
        dataset_id="alpaca-aapl",
        revision="mock-rth",
        query=AlpacaBarQuery(start=date(2024, 1, 2), end=date(2024, 1, 3), timeframe="1Min"),
        created_at=datetime(2024, 1, 10, tzinfo=UTC),
    )
    return store.load("alpaca-aapl", "mock-rth")


def test_virtual_cash_accounting_and_determinism(alpaca_env: None, tmp_path: Path) -> None:
    source = _ingest(tmp_path)
    first = run_virtual_portfolio(source, starting_cash=VIRTUAL_CASH_USD, run_id="demo")
    second = run_virtual_portfolio(source, starting_cash=VIRTUAL_CASH_USD, run_id="demo")
    assert first.result.starting_cash == 10_000.0
    assert first.result.fills == 1
    assert first.report.holdings["AAPL"] == 1
    fill = next(row for row in first.result.journal.records if row["type"] == "fill")
    assert fill["fill_price"] == 102.0
    assert fill["execution_time"] > fill["decision_time"]
    assert first.report.ending_cash == pytest.approx(10_000.0 - 102.0)
    assert first.report.marked_equity == pytest.approx(first.report.ending_cash + 103.0)
    assert first.report.unrealized_pnl == pytest.approx(1.0)
    assert first.report.realized_pnl == pytest.approx(0.0)
    assert first.report.simulated is True
    disclaimer = first.report.price_disclaimer.lower()
    assert "mocked" in disclaimer or "not live" in disclaimer
    assert first.result.journal.dumps() == second.result.journal.dumps()
    assert first.report.to_dict() == second.report.to_dict()


def test_future_bar_does_not_leak_into_first_decision(alpaca_env: None, tmp_path: Path) -> None:
    source = _ingest(tmp_path)
    views: list[MarketView] = []

    class Spy(BuyOnceNextOpenStrategy):
        def on_as_of(self, view: MarketView) -> Signal | None:
            views.append(view)
            return super().on_as_of(view)

    run_virtual_portfolio(source, strategy=Spy(), run_id="spy")
    first = views[0]
    assert first.last_bar() is not None
    assert first.last_bar().high == 101.5
    assert first.last_bar().close == 101.2
    assert all(bar.available_time <= first.as_of for bar in first.bars)
    clock = EventClock(source.stream())
    later = clock.visible(datetime(2024, 1, 3, 22, tzinfo=UTC))
    assert any("2024-01-03" in event.event_id for event in later)


def test_insufficient_cash_is_rejected(alpaca_env: None, tmp_path: Path) -> None:
    source = _ingest(tmp_path)
    settings_risk = RiskManager(
        limits=RiskLimits(max_position_qty=1_000, max_order_qty=1_000, max_notional=1_000_000.0)
    )
    demo = run_virtual_portfolio(
        source,
        starting_cash=VIRTUAL_CASH_USD,
        quantity=200,
        risk=settings_risk,
        execution=ExecutionModel(),
        run_id="no-cash",
    )
    assert demo.result.fills == 0
    assert demo.result.rejected >= 1
    assert demo.report.ending_cash == 10_000.0
    assert demo.report.holdings == {}
    assert any(
        row["type"] == "decision" and row["approved"] is False
        for row in demo.result.journal.records
    )


def test_commissions_reduce_cash(alpaca_env: None, tmp_path: Path) -> None:
    source = _ingest(tmp_path)
    demo = run_virtual_portfolio(
        source,
        execution=ExecutionModel(commission_per_share=1.0, slippage_bps=0),
        run_id="fee",
    )
    assert demo.result.fills == 1
    assert demo.report.ending_cash == pytest.approx(10_000.0 - 102.0 - 1.0)
