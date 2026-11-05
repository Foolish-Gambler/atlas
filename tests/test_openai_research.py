from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas.cli import main
from atlas.ingest.edgar import EdgarQuery
from atlas.ingest.edgar import adapter_for_tests as edgar_adapter
from atlas.ingest.filing_docs import FilingDocumentQuery, adapter_for_tests
from atlas.ingest.snapshot import SnapshotStore
from atlas.research.engine import ResearchEngine
from atlas.research.errors import (
    LlmNetworkDisabledError,
    ResearchBudgetError,
    ResearchProviderError,
    ResearchRejectedError,
)
from atlas.research.models import ResearchQuery
from atlas.research.openai_provider import OpenAIProviderSettings, OpenAIResearchProvider
from atlas.research.schema import ResearchClaimModel, ResearchOutputModel
from atlas.validation import InvalidInputError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ingest" / "edgar"
HTML = (FIXTURE_DIR / "aapl_10q_excerpt.html").read_text(encoding="utf-8")
CANONICAL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"
AS_OF = datetime(2026, 8, 2, tzinfo=UTC)
ACCESSION = "0000320193-26-000020"
REAL_SNAPSHOTS = Path(__file__).resolve().parents[1] / "data" / "snapshots"
REAL_DOCS = REAL_SNAPSHOTS / "aapl-10q-doc-real" / "2026-11-05-02" / "manifest.json"


@pytest.fixture
def edgar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_EDGAR_USER_AGENT", "ATLAS tests contact@example.com")


def _pages() -> dict:
    return json.loads((FIXTURE_DIR / "aapl_pages.json").read_text(encoding="utf-8"))


def _snapshots(tmp_path: Path) -> SnapshotStore:
    store = SnapshotStore(tmp_path)
    pages = _pages()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json=pages["tickers"])
        if "/submissions/" in path:
            return httpx.Response(200, json=pages["submissions"])
        if "/companyfacts/" in path:
            return httpx.Response(200, json=pages["companyfacts"])
        return httpx.Response(404, json={"error": path})

    edgar_adapter(handler).ingest(
        store, dataset_id="edgar-aapl", revision="meta-1", query=EdgarQuery(symbol="AAPL")
    )
    source = store.load("edgar-aapl", "meta-1")
    recorded = {CANONICAL: {"status": 200, "headers": {"content-type": "text/html"}, "body": HTML}}
    adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        store,
        source,
        dataset_id="edgar-docs",
        revision="1",
        query=FilingDocumentQuery(accession=ACCESSION, require_success=True),
        recorded=recorded,
    )
    return store


def _query(**overrides: object) -> ResearchQuery:
    payload = {"symbol": "AAPL", "research_as_of": AS_OF, "accession": ACCESSION}
    payload.update(overrides)
    return ResearchQuery(**payload)  # type: ignore[arg-type]


def _pack(store: SnapshotStore):
    return ResearchEngine(store).retriever.retrieve(
        _query(),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
    )


def _valid_output(pack) -> ResearchOutputModel:
    chunk = pack.chunks[0]
    span = chunk.text[:80]
    end = chunk.text_start + len(span)
    fact = pack.facts[0]
    return ResearchOutputModel(
        thesis="Cited filing excerpts only. No rating or order is issued.",
        observations=["Revenue and profitability are discussed in the cited excerpt."],
        growth_drivers=["Services growth is mentioned only if present in evidence."],
        risks=["Material risks are limited to cited Item 1A text when retrieved."],
        bullish_hypotheses=["If cited demand holds, results may stay resilient."],
        bearish_hypotheses=["If cited legal or cost risks worsen, results could weaken."],
        counterarguments=["Excerpts are incomplete and not semantically verified."],
        missing_information=["Full filing text was not supplied."],
        uncertainties=["Mechanical citations are not proof of a complete reading."],
        claims=[
            ResearchClaimModel(
                claim_id="c1",
                text=f"Excerpt: {span}",
                category="performance",
                evidence_refs=[chunk.chunk_id],
                quoted_span=span,
                chunk_id=chunk.chunk_id,
                text_start=chunk.text_start,
                text_end=end,
            ),
            ResearchClaimModel(
                claim_id="c2",
                text=f"XBRL {fact.concept} equals {fact.value} {fact.unit}.",
                category="performance",
                evidence_refs=[fact.ref_id],
                fact_id=fact.ref_id,
                asserted_value=fact.value,
                asserted_unit=fact.unit,
                asserted_period=fact.period_kind,
                asserted_accession=fact.accession,
            ),
        ],
        evidence_refs=[chunk.chunk_id, fact.ref_id],
    )


@dataclass
class FakeUsage:
    input_tokens: int = 11
    output_tokens: int = 22


@dataclass
class FakeResponse:
    output_parsed: Any
    id: str = "resp_test"
    status: str = "completed"
    model: str = "test-model"
    usage: FakeUsage = field(default_factory=FakeUsage)
    output: tuple[Any, ...] = ()


class FakeClient:
    def __init__(
        self, response: FakeResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.responses = self

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _provider(client: FakeClient, **overrides: object) -> OpenAIResearchProvider:
    settings = replace(OpenAIProviderSettings(model_id="test-model"), **overrides)
    return OpenAIResearchProvider(
        settings=settings,
        api_key="sk-test",
        client=client,
        allow_network=True,
    )


def test_valid_structured_openai_response_is_published(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    client = FakeClient(FakeResponse(_valid_output(pack)))
    engine = ResearchEngine(store, provider=_provider(client))
    generated = datetime(2026, 11, 5, 18, tzinfo=UTC)
    result = engine.analyze(
        _query(),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
        output_dataset="research-openai",
        output_revision="1",
        created_at=generated,
    )
    assert result.published is True
    assert result.artifact.llm_called is True
    assert result.artifact.simulated is False
    assert result.artifact.provider_id == "openai"
    assert result.artifact.response_id == "resp_test"
    assert result.artifact.input_tokens == 11
    assert result.artifact.request_count == 1
    assert result.artifact.time.available_time == result.artifact.generated_at
    assert result.artifact.time.available_time >= AS_OF
    assert all(not claim.semantically_verified for claim in result.artifact.claims)
    kwargs = client.calls[0]
    assert kwargs["store"] is False
    assert kwargs["model"] == "test-model"
    assert "tools" not in kwargs
    loaded = store.load("research-openai", "1")
    assert loaded.manifest.metadata["llm_called"] is True
    assert loaded.manifest.vendor == "openai"


def test_openai_does_not_fall_back_to_mock(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    client = FakeClient(error=APITimeoutError("timeout"))
    engine = ResearchEngine(store, provider=_provider(client))
    with pytest.raises(ResearchProviderError, match="timed out"):
        engine.analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-openai",
            output_revision="timeout",
        )
    assert not (tmp_path / "research-openai" / "timeout").exists()


class APITimeoutError(Exception):
    pass


class RateLimitError(Exception):
    pass


def test_openai_rate_limit_is_fail_closed(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    client = FakeClient(error=RateLimitError("429"))
    with pytest.raises(ResearchProviderError, match="rate limit"):
        _provider(client).analyze(_query(), _pack(store))


def test_openai_refusal_is_not_published(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)

    @dataclass
    class Refusal:
        type: str = "refusal"
        refusal: str = "cannot help"

    @dataclass
    class Message:
        content: tuple[Any, ...] = (Refusal(),)

    client = FakeClient(FakeResponse(None, status="completed", output=(Message(),)))
    with pytest.raises(ResearchProviderError, match="refused"):
        _provider(client).analyze(_query(), _pack(store))
    assert not (tmp_path / "research-openai").exists()


def test_incomplete_output_is_rejected(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    client = FakeClient(FakeResponse(None, status="incomplete"))
    with pytest.raises(ResearchProviderError, match="incomplete"):
        _provider(client).analyze(_query(), _pack(store))


def test_invalid_model_output_is_not_published(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    parsed = _valid_output(pack)
    parsed = parsed.model_copy(
        update={
            "claims": [
                ResearchClaimModel(
                    claim_id="bad",
                    text="Sales exploded to 999 trillion.",
                    category="performance",
                    evidence_refs=["fact:missing"],
                )
            ]
        }
    )
    client = FakeClient(FakeResponse(parsed))
    with pytest.raises(ResearchRejectedError):
        ResearchEngine(store, provider=_provider(client)).analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-openai",
            output_revision="invalid",
        )
    assert not (tmp_path / "research-openai" / "invalid").exists()


def test_wrong_chunk_range_from_model_is_rejected(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    chunk = pack.chunks[0]
    parsed = _valid_output(pack)
    parsed = parsed.model_copy(
        update={
            "claims": [
                ResearchClaimModel(
                    claim_id="c1",
                    text="Wrong offsets.",
                    category="performance",
                    evidence_refs=[chunk.chunk_id],
                    quoted_span=chunk.text[:40],
                    chunk_id=chunk.chunk_id,
                    text_start=chunk.text_start + 10_000,
                    text_end=chunk.text_start + 10_040,
                )
            ]
        }
    )
    client = FakeClient(FakeResponse(parsed))
    with pytest.raises(ResearchRejectedError):
        ResearchEngine(store, provider=_provider(client)).analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-openai",
            output_revision="range",
        )


def test_fabricated_financial_number_is_rejected(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    fact = pack.facts[0]
    parsed = _valid_output(pack)
    parsed = parsed.model_copy(
        update={
            "claims": [
                ResearchClaimModel(
                    claim_id="c2",
                    text="Invented revenue.",
                    category="performance",
                    evidence_refs=[fact.ref_id],
                    fact_id=fact.ref_id,
                    asserted_value=fact.value + 1_000_000,
                    asserted_unit=fact.unit,
                    asserted_accession=fact.accession,
                )
            ]
        }
    )
    client = FakeClient(FakeResponse(parsed))
    with pytest.raises(ResearchRejectedError, match="fabricated_value"):
        ResearchEngine(store, provider=_provider(client)).analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-openai",
            output_revision="number",
        )


def test_future_dated_evidence_in_model_output_is_rejected(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    chunk = pack.chunks[0]
    parsed = _valid_output(pack)
    parsed = parsed.model_copy(
        update={
            "claims": [
                ResearchClaimModel(
                    claim_id="c1",
                    text=chunk.text[:80],
                    category="performance",
                    evidence_refs=[chunk.chunk_id],
                    quoted_span=chunk.text[:80],
                    chunk_id=chunk.chunk_id,
                    text_start=chunk.text_start,
                    text_end=chunk.text_start + 80,
                )
            ]
        }
    )
    client = FakeClient(FakeResponse(parsed))
    with pytest.raises(ResearchRejectedError):
        ResearchEngine(store, provider=_provider(client)).analyze(
            _query(research_as_of=datetime(2020, 1, 1, tzinfo=UTC)),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-openai",
            output_revision="future",
        )


def test_missing_api_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_OPENAI_MODEL", raising=False)
    with pytest.raises(InvalidInputError, match="ATLAS_OPENAI_MODEL"):
        OpenAIResearchProvider.from_env(allow_network=True, api_key=None)
    provider = OpenAIResearchProvider(
        settings=OpenAIProviderSettings(model_id="test-model"),
        api_key="",
        allow_network=True,
        client=FakeClient(FakeResponse(_valid_output_empty())),
    )
    with pytest.raises(InvalidInputError, match="OPENAI_API_KEY"):
        provider.analyze(_query(), _empty_pack())


def _valid_output_empty() -> ResearchOutputModel:
    return ResearchOutputModel(thesis="No evidence was supplied. No rating is issued.")


def _empty_pack():
    from atlas.research.models import EvidencePack

    return EvidencePack(
        query=_query(),
        documents=(),
        chunks=(),
        facts=(),
        document_dataset="docs",
        document_revision="1",
        financial_dataset="fin",
        financial_revision="1",
    )


def test_token_and_request_budget(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    client = FakeClient(FakeResponse(_valid_output(pack)))
    tight = _provider(client, max_input_tokens=1)
    with pytest.raises(ResearchBudgetError, match="estimated input tokens"):
        tight.analyze(_query(), pack)
    provider = _provider(client, max_requests=1)
    provider._requests = 1
    with pytest.raises(ResearchBudgetError, match="request budget"):
        provider.analyze(_query(), pack)


def test_missing_network_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_ALLOW_LLM_NETWORK", raising=False)
    with pytest.raises(LlmNetworkDisabledError):
        OpenAIResearchProvider(
            settings=OpenAIProviderSettings(model_id="test-model"),
            api_key="sk-test",
        ).analyze(_query(), _empty_pack())


def test_dry_run_does_not_call_openai(
    edgar_env: None, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _snapshots(tmp_path)
    monkeypatch.delenv("ATLAS_ALLOW_LLM_NETWORK", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main(
        [
            "research",
            "analyze",
            "--root",
            str(store.root),
            "--symbol",
            "AAPL",
            "--document-dataset",
            "edgar-docs",
            "--document-revision",
            "1",
            "--financial-dataset",
            "edgar-aapl",
            "--financial-revision",
            "meta-1",
            "--provider",
            "openai",
            "--accession",
            ACCESSION,
            "--as-of",
            AS_OF.isoformat(),
            "--dry-run",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["llm_called"] is False
    assert payload["dry_run"] is True
    assert payload["expected_request_count"] == 1
    assert payload["estimated_input_tokens"] > 0
    assert payload["store_responses"] is False


def test_cost_recorded_only_with_pricing_config(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    pack = _pack(store)
    client = FakeClient(FakeResponse(_valid_output(pack)))
    provider = _provider(client, input_price_per_million=1.0, output_price_per_million=2.0)
    result = provider.analyze(_query(), pack)
    assert result.estimated_cost == pytest.approx((11 * 1.0 + 22 * 2.0) / 1_000_000)


def test_research_package_still_isolated_from_trading() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "atlas" / "research"
    forbidden = ("atlas.paper_trading", "atlas.risk", "PaperBroker", "RiskManager")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} contains {token}"


@pytest.mark.skipif(not REAL_DOCS.is_file(), reason="local Apple snapshots are not present")
def test_dry_run_estimates_real_apple_payload() -> None:
    store = SnapshotStore(REAL_SNAPSHOTS)
    stats = ResearchEngine(store).estimate(
        ResearchQuery(symbol="AAPL", research_as_of=AS_OF, accession=ACCESSION),
        document_dataset="aapl-10q-doc-real",
        document_revision="2026-11-05-02",
        financial_dataset="aapl-edgar-real",
        financial_revision="2026-10-28-02",
    )
    assert stats["llm_called"] is False
    assert stats["expected_request_count"] == 1
    assert stats["chunks"] >= 1
