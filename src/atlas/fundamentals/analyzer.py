"""Fundamental ratios computed only from caller-supplied statements."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain import DataProvenance


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    symbol: str
    earnings_per_share: float
    book_value_per_share: float
    price: float
    provenance: DataProvenance = "caller_supplied"


@dataclass(frozen=True, slots=True)
class FundamentalScore:
    symbol: str
    pe_ratio: float | None
    pb_ratio: float | None
    score: float
    provenance: DataProvenance


class FundamentalAnalyzer:
    def analyze(self, snapshot: FinancialSnapshot) -> FundamentalScore:
        if snapshot.price <= 0:
            raise ValueError("price must be positive.")
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
