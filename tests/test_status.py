from __future__ import annotations

from atlas.config import AtlasSettings
from atlas.status import collect_status


def test_collect_status_lists_all_modules(settings: AtlasSettings) -> None:
    status = collect_status(settings)
    names = {module.name for module in status.modules}
    assert names == {
        "market_data",
        "news",
        "fundamentals",
        "forecasting",
        "risk",
        "backtesting",
        "paper_trading",
    }
    assert status.live_broker_connected is False
    assert status.trading_mode.value == "paper"
    assert all(module.ready for module in status.modules)
