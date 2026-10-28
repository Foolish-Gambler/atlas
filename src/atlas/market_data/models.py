"""Market data records. Provenance is required so simulated series stay labeled."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from atlas.domain import DataProvenance


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    as_of: datetime
    provenance: DataProvenance
    disclaimer: str


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
