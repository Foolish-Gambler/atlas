"""Fundamental ratios computed only from caller-supplied statements."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain import DataProvenance
from atlas.validation import normalize_symbol, require_finite, require_positive_finite


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    symbol: str
    earnings_per_share: float
    book_value_per_share: float
    price: float
    provenance: DataProvenance = "caller_supplied"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "price", require_positive_finite(self.price, "price"))
        object.__setattr__(
            self,
            "earnings_per_share",
            require_finite(self.earnings_per_share, "earnings_per_share"),
        )
        object.__setattr__(
            self,
            "book_value_per_share",
            require_finite(self.book_value_per_share, "book_value_per_share"),
        )


@dataclass(frozen=True, slots=True)
class FundamentalScore:
    symbol: str
    pe_ratio: float | None
    pb_ratio: float | None
    score: float
    provenance: DataProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


class FundamentalAnalyzer:
    def analyze(self, snapshot: FinancialSnapshot) -> FundamentalScore:
        pe = (
            snapshot.price / snapshot.earnings_per_share
            if snapshot.earnings_per_share > 0
            else None
        )
        pb = (
            snapshot.price / snapshot.book_value_per_share
            if snapshot.book_value_per_share > 0
            else None
        )
        score = 0.0
        if pe is not None and pe < 20:
            score += 0.5
        if pb is not None and pb < 3:
            score += 0.5
        return FundamentalScore(
            symbol=snapshot.symbol,
            pe_ratio=pe,
            pb_ratio=pb,
            score=score,
            provenance=snapshot.provenance,
        )
