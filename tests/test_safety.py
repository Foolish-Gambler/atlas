from __future__ import annotations

import pytest

from atlas.config import AtlasSettings
from atlas.domain import TradingMode
from atlas.safety import SafetyError, assert_no_live_broker, assert_paper_only


def test_assert_paper_only_accepts_paper(settings: AtlasSettings) -> None:
    assert assert_paper_only(settings) is TradingMode.PAPER


def test_assert_paper_only_rejects_live() -> None:
    live = AtlasSettings(
        trading_mode=TradingMode.LIVE,
        starting_cash=1.0,
        live_broker_connected=False,
    )
    with pytest.raises(SafetyError, match="Live brokerage mode is disabled"):
        assert_paper_only(live)


def test_load_rejects_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_TRADING_MODE", "live")
    with pytest.raises(ValueError, match="cannot enable live trading"):
        AtlasSettings.load()


def test_live_broker_flag_is_rejected() -> None:
    with pytest.raises(SafetyError):
        assert_no_live_broker(True)
