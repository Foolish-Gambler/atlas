from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlas.backtesting import ExecutionModel, NextOpenBuyStrategy, PitBacktestEngine
from atlas.backtesting.view import MarketView
from atlas.config import AtlasSettings
from atlas.contracts import EventClock
from atlas.domain import Signal
from atlas.ingest import LocalFixtureSource
from atlas.risk import RiskManager

FIXTURE = Path(__file__).parent / "fixtures" / "pit" / "m1_sim_a.json"


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
    assert fills[0]["origin_event_id"] != fills[0]["execute_event_id"]
    assert result.cancelled == 1


def test_strategy_cannot_see_future_filings_or_research(settings: AtlasSettings) -> None:
    source = LocalFixtureSource(FIXTURE)
    spy = SpyStrategy()
    PitBacktestEngine(settings=settings).run(source, spy, run_id="run-spy")
    first = spy.views[0]
    assert first.as_of == datetime(2024, 1, 2, 21, tzinfo=UTC)
    assert first.filings == ()
    assert first.research == ()
    assert first.predictions == ()
    assert spy.views[2].research == ()
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


def test_local_fixture_rejects_vendor_claim(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        '{"dataset_id":"x","revision":"1","source_kind":"vendor_read_only","bars":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        LocalFixtureSource(path)
