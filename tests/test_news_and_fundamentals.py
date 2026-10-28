from __future__ import annotations

import pytest

from atlas.fundamentals import FinancialSnapshot, FundamentalAnalyzer
from atlas.news import NewsAnalyzer, NewsItem


def test_news_analyzer_scores_provided_headlines() -> None:
    analyzer = NewsAnalyzer()
    score = analyzer.score(
        [
            NewsItem(symbol="SIM-A", headline="Record growth and profit"),
            NewsItem(symbol="SIM-A", headline="Upgrade on growth"),
        ]
    )
    assert score.provenance == "caller_supplied"
    assert score.label == "positive"
    assert score.score > 0


def test_news_analyzer_rejects_mixed_symbols() -> None:
    with pytest.raises(ValueError, match="same symbol"):
        NewsAnalyzer().score(
            [
                NewsItem(symbol="SIM-A", headline="growth"),
                NewsItem(symbol="SIM-B", headline="growth"),
            ]
        )


def test_fundamentals_use_caller_numbers_only() -> None:
    result = FundamentalAnalyzer().analyze(
        FinancialSnapshot(
            symbol="SIM-A",
            earnings_per_share=5.0,
            book_value_per_share=40.0,
            price=80.0,
        )
    )
    assert result.pe_ratio == 16.0
    assert result.pb_ratio == 2.0
    assert result.score == 1.0
    assert result.provenance == "caller_supplied"
