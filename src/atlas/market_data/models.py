"""Market data records. Provenance is required so simulated series stay labeled."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from atlas.domain import DataProvenance
from atlas.validation import normalize_symbol, require_int, require_positive_finite


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    as_of: datetime
    provenance: DataProvenance
    disclaimer: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "price", require_positive_finite(self.price, "price"))


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    provenance: DataProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "open", require_positive_finite(self.open, "open"))
        object.__setattr__(self, "high", require_positive_finite(self.high, "high"))
        object.__setattr__(self, "low", require_positive_finite(self.low, "low"))
        object.__setattr__(self, "close", require_positive_finite(self.close, "close"))
        volume = require_int(self.volume, "volume")
        if volume < 0:
            raise ValueError("volume must be non-negative.")
        object.__setattr__(self, "volume", volume)
