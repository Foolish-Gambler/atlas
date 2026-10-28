from __future__ import annotations

import pytest

from atlas.config import AtlasSettings
from atlas.validation import InvalidInputError


def test_starting_cash_rejects_nan_and_inf() -> None:
    with pytest.raises(InvalidInputError):
        AtlasSettings(starting_cash=float("nan"))
    with pytest.raises(InvalidInputError):
        AtlasSettings(starting_cash=float("inf"))
    with pytest.raises(InvalidInputError):
        AtlasSettings(starting_cash=0.0)
    with pytest.raises(InvalidInputError):
        AtlasSettings(starting_cash=-100.0)


def test_load_rejects_non_finite_starting_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_STARTING_CASH", "nan")
    with pytest.raises(InvalidInputError):
        AtlasSettings.load()


def test_settings_cannot_enable_live_broker_flag() -> None:
    with pytest.raises(InvalidInputError, match="live_broker_connected"):
        AtlasSettings(live_broker_connected=True)
