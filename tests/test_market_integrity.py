from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atlas.market_data.models import Bar, Quote
from atlas.validation import InvalidInputError


def test_bar_rejects_invalid_ohlc() -> None:
    ts = datetime(2024, 1, 2, tzinfo=UTC)
    with pytest.raises(InvalidInputError, match="high"):
        Bar(
            symbol="SIM-A",
            timestamp=ts,
            open=10.0,
            high=9.0,
            low=8.0,
            close=9.5,
            volume=1,
            provenance="simulated",
        )
    with pytest.raises(InvalidInputError, match="low"):
        Bar(
            symbol="SIM-A",
            timestamp=ts,
            open=10.0,
            high=12.0,
            low=11.0,
            close=10.5,
            volume=1,
            provenance="simulated",
        )


def test_bar_and_quote_require_timezone_aware_timestamps() -> None:
    naive = datetime(2024, 1, 2)
    with pytest.raises(InvalidInputError, match="timezone-aware"):
        Bar(
            symbol="SIM-A",
            timestamp=naive,
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1,
            provenance="simulated",
        )
    with pytest.raises(InvalidInputError, match="timezone-aware"):
        Quote(
            symbol="SIM-A",
            price=10.0,
            as_of=naive,
            provenance="simulated",
            disclaimer="simulated",
        )


def test_valid_bar_is_accepted() -> None:
    bar = Bar(
        symbol="sim-a",
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        volume=3,
        provenance="simulated",
    )
    assert bar.symbol == "SIM-A"
    assert bar.timestamp.tzinfo is not None
