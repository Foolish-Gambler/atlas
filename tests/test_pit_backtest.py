from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.backtesting import (
    ExecutionModel,
    NextOpenBuyStrategy,
    PitBacktestEngine,
    SingleSymbolError,
)
from atlas.backtesting.view import MarketView
from atlas.config import AtlasSettings
from atlas.contracts import EventClock
from atlas.domain import Signal, TradingMode
from atlas.ingest import LocalFixtureSource
from atlas.risk import RiskManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pit"
FIXTURE = FIXTURE_DIR / "m1_sim_a.json"
DELAYED = FIXTURE_DIR / "m1_delayed.json"
MIXED = FIXTURE_DIR / "m1_mixed_symbols.json"
WEEKEND = FIXTURE_DIR / "m1_weekend.json"
DST = FIXTURE_DIR / "m1_dst.json"


class SpyStrategy:
    def __init__(self) -> None:
        self.views: list[MarketView] = []
        self._inner = NextOpenBuyStrategy(quantity=1)

    def on_as_of(self, view: MarketView) -> Signal | None:
        self.views.append(view)
        return self._inner.on_as_of(view)


def test_pit_fills_next_open_not_same_close(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(FIXTURE)
    result = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="run-a"
    )
    fills = [row for row in result.journal.records if row["type"] == "fill"]
    assert len(fills) == 3
    assert fills[0]["reference_price"] == 100.0
    assert fills[0]["open"] == 102.0
    assert fills[0]["fill_price"] == 102.0
    assert fills[0]["origin_event_id"].startswith("bar-close:")
    assert fills[0]["execute_event_id"].startswith("bar-open:")
    assert fills[0]["execution_time"] == "2024-01-03T14:30:00+00:00"
    assert fills[0]["decision_time"] == "2024-01-02T21:00:00+00:00"
    assert fills[0]["execution_time"] > fills[0]["decision_time"]
    assert result.cancelled == 1


def test_strategy_cannot_see_future_filings_or_research(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(FIXTURE)
    spy = SpyStrategy()
    PitBacktestEngine(settings=settings).run(source, spy, run_id="run-spy")
    first = spy.views[0]
    assert first.as_of == datetime(2024, 1, 2, 21, tzinfo=UTC)
    assert first.last_bar() is not None
    assert first.last_bar().high == 101.0
    assert first.last_bar().low == 99.0
    assert first.last_bar().close == 100.0
    assert all(bar.available_time <= first.as_of for bar in first.bars)
    assert all(bar.close_time <= first.as_of for bar in first.bars)
    assert first.filings == ()
    assert first.research == ()
    assert first.predictions == ()
    assert spy.views[2].research == ()
    assert spy.views[2].last_bar().high == 102.0
    last = spy.views[-1]
    assert last.as_of == datetime(2024, 1, 5, 21, tzinfo=UTC)
    assert last.filings == ()
    assert last.research[0].artifact_id == "r-late"
    assert last.predictions[0].prediction_id == "p-late"
    clock = EventClock(source.stream())
    after_close = clock.visible(datetime(2024, 1, 5, 22, tzinfo=UTC))
    assert any(event.event_id == "filing:000-future-leak" for event in after_close)


def test_costs_and_equity_accounting(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(FIXTURE)
    cheap = PitBacktestEngine(settings=settings, risk=RiskManager()).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="cheap"
    )
    costly = PitBacktestEngine(
        settings=settings,
        risk=RiskManager(),
        execution=ExecutionModel(slippage_bps=100, commission_per_share=0.5),
    ).run(source, NextOpenBuyStrategy(quantity=1), run_id="costly")
    assert costly.ending_cash < cheap.ending_cash
    fills = [row for row in costly.journal.records if row["type"] == "fill"]
    assert fills[0]["fill_price"] == pytest.approx(102.0 * 1.01)
    assert fills[0]["commission"] == pytest.approx(0.5)
    assert cheap.ending_equity > 0
    assert len(cheap.equity_curve) == 4


def test_reproducible_journal(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(FIXTURE)
    first = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="same"
    )
    second = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="same"
    )
    assert first.journal.dumps() == second.journal.dumps()
    assert first.ending_cash == second.ending_cash
    assert first.fills == second.fills


def test_mixed_symbol_fixtures_are_rejected(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(MIXED)
    with pytest.raises(SingleSymbolError, match="single symbol"):
        PitBacktestEngine(settings=settings).run(source, run_id="mixed")


def test_delayed_bar_does_not_fill_at_earlier_open(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(DELAYED)
    result = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="delayed"
    )
    fills = [row for row in result.journal.records if row["type"] == "fill"]
    assert len(fills) == 1
    assert fills[0]["open"] == 105.0
    assert fills[0]["fill_price"] == 105.0
    assert fills[0]["execution_time"] > fills[0]["decision_time"]
    assert fills[0]["execute_event_id"].startswith("bar-open:")
    assert "2024-01-03T14:30:00" not in fills[0]["execute_event_id"]
    assert fills[0]["execution_time"] == "2024-01-04T14:30:00+00:00"


def test_insufficient_commission_rolls_back(settings: AtlasSettings) -> None:
    tight = AtlasSettings(
        trading_mode=TradingMode.PAPER,
        starting_cash=100.0,
        live_broker_connected=False,
    )
    source = LocalFixtureSource(FIXTURE)
    result = PitBacktestEngine(
        settings=tight,
        execution=ExecutionModel(commission_per_share=1.0),
    ).run(source, NextOpenBuyStrategy(quantity=1), run_id="no-fee-cash")
    assert result.fills == 0
    assert result.ending_cash == 100.0
    assert any(row["type"] == "reject" for row in result.journal.records)


def test_weekend_gap_fills_monday_open(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(WEEKEND)
    result = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="weekend"
    )
    fills = [row for row in result.journal.records if row["type"] == "fill"]
    assert len(fills) == 1
    assert fills[0]["fill_price"] == 104.0
    assert fills[0]["decision_time"] == "2024-01-05T21:00:00+00:00"
    assert fills[0]["execution_time"] == "2024-01-08T14:30:00+00:00"


def test_dst_transition_uses_eastern_open(settings: AtlasSettings) -> None:
    # 2024-03-10 US spring-forward: Friday is EST (-5), Monday is EDT (-4).
    est = timezone(timedelta(hours=-5))
    edt = timezone(timedelta(hours=-4))
    friday_open = datetime(2024, 3, 8, 9, 30, tzinfo=est)
    monday_open = datetime(2024, 3, 11, 9, 30, tzinfo=edt)
    assert friday_open.utcoffset() == timedelta(hours=-5)
    assert monday_open.utcoffset() == timedelta(hours=-4)
    source = LocalFixtureSource(DST)
    bars = source.all_bars()
    assert bars[0].open_time == friday_open
    assert bars[1].open_time == monday_open
    result = PitBacktestEngine(settings=settings).run(
        source, NextOpenBuyStrategy(quantity=1), run_id="dst"
    )
    fills = [row for row in result.journal.records if row["type"] == "fill"]
    assert len(fills) == 1
    assert fills[0]["fill_price"] == 108.0
    assert datetime.fromisoformat(fills[0]["execution_time"]) == monday_open


def test_local_fixture_rejects_vendor_claim(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        '{"dataset_id":"x","revision":"1","source_kind":"vendor_read_only","bars":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        LocalFixtureSource(path)
