"""Paper portfolio reporting. Does not change broker state."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.backtesting.pit import PitBacktestResult
from atlas.domain import SignalSide
from atlas.paper_trading.broker import PaperFill


@dataclass(frozen=True, slots=True)
class PaperPortfolioReport:
    starting_cash: float
    ending_cash: float
    holdings: dict[str, int]
    marked_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fills: int
    simulated: bool
    execution_eligible: bool
    price_disclaimer: str

    def to_dict(self) -> dict[str, object]:
        return {
            "starting_cash": self.starting_cash,
            "ending_cash": self.ending_cash,
            "holdings": dict(self.holdings),
            "marked_equity": self.marked_equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "fills": self.fills,
            "simulated": self.simulated,
            "execution_eligible": self.execution_eligible,
            "price_disclaimer": self.price_disclaimer,
        }


def report_from_fills(
    result: PitBacktestResult,
    fills: tuple[PaperFill, ...] | list[dict[str, object]],
    *,
    execution_eligible: bool,
    price_disclaimer: str,
) -> PaperPortfolioReport:
    cost_basis = 0.0
    remaining = dict(result.ending_positions)
    lots: dict[str, list[tuple[int, float]]] = {}
    for fill in fills:
        symbol, side, qty, price = _fill_fields(fill)
        if side == SignalSide.BUY.value or side is SignalSide.BUY:
            lots.setdefault(symbol, []).append((qty, price))
        else:
            left = qty
            held = lots.setdefault(symbol, [])
            while left and held:
                lot_qty, lot_price = held[0]
                take = min(lot_qty, left)
                left -= take
                if take == lot_qty:
                    held.pop(0)
                else:
                    held[0] = (lot_qty - take, lot_price)
    for symbol, qty in remaining.items():
        held_cost = 0.0
        need = qty
        for lot_qty, lot_price in lots.get(symbol, ()):
            take = min(lot_qty, need)
            held_cost += take * lot_price
            need -= take
            if need == 0:
                break
        cost_basis += held_cost
    market_value = result.ending_equity - result.ending_cash
    unrealized = market_value - cost_basis
    total_pnl = result.ending_equity - result.starting_cash
    realized = total_pnl - unrealized
    return PaperPortfolioReport(
        starting_cash=result.starting_cash,
        ending_cash=result.ending_cash,
        holdings=dict(result.ending_positions),
        marked_equity=result.ending_equity,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        fills=result.fills,
        simulated=True,
        execution_eligible=execution_eligible,
        price_disclaimer=price_disclaimer,
    )


def _fill_fields(fill: PaperFill | dict[str, object]) -> tuple[str, object, int, float]:
    if isinstance(fill, dict):
        return (
            str(fill["symbol"]),
            fill["side"],
            int(fill["quantity"]),
            float(fill["fill_price"]),
        )
    return fill.symbol, fill.side, fill.quantity, fill.price
