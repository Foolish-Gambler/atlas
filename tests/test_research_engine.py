from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from atlas.cli import build_parser, main
from atlas.contracts import (
    FilingDocumentRecord,
    FilingTextChunk,
    Provenance,
    ResearchArtifact,
    ResearchClaim,
    SourceKind,
    TimeIndex,
    XbrlFactRecord,
)
from atlas.ingest.edgar import EdgarQuery
from atlas.ingest.edgar import adapter_for_tests as edgar_adapter
from atlas.ingest.filing_docs import FilingDocumentQuery, adapter_for_tests
from atlas.ingest.snapshot import RevisionExistsError, SnapshotIntegrityError, SnapshotStore
from atlas.research.engine import ResearchEngine
from atlas.research.models import EvidencePack, ResearchQuery
from atlas.research.provider import MOCK_BANNER, OpenAIResearchProvider, provider_from_name
from atlas.research.retriever import SnapshotEvidenceRetriever
from atlas.research.validator import EvidenceValidator
from atlas.validation import InvalidInputError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ingest" / "edgar"
HTML = (FIXTURE_DIR / "aapl_10q_excerpt.html").read_text(encoding="utf-8")
CANONICAL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"
AS_OF = datetime(2026, 8, 2, tzinfo=UTC)
ACCESSION = "0000320193-26-000020"


@pytest.fixture
def edgar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_EDGAR_USER_AGENT", "ATLAS tests contact@example.com")


def _pages() -> dict:
    return json.loads((FIXTURE_DIR / "aapl_pages.json").read_text(encoding="utf-8"))


def _edgar_handler():
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

    return handler


def _snapshots(tmp_path: Path) -> SnapshotStore:
    store = SnapshotStore(tmp_path)
    edgar_adapter(_edgar_handler()).ingest(
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
    payload = {
        "symbol": "AAPL",
        "research_as_of": AS_OF,
        "accession": ACCESSION,
    }
    payload.update(overrides)
    return ResearchQuery(**payload)  # type: ignore[arg-type]


def _prov() -> Provenance:
    return Provenance(source_kind=SourceKind.SIMULATED, dataset_id="d", revision="r")


def _time(day: int) -> TimeIndex:
    stamp = datetime(2026, 8, day, tzinfo=UTC)
    return TimeIndex(event_time=stamp, available_time=stamp, ingested_time=stamp)


def test_research_analyze_parser_accepts_mock_provider() -> None:
    args = build_parser().parse_args(
        [
            "research",
            "analyze",
            "--root",
            "snapshots",
            "--symbol",
            "AAPL",
            "--document-dataset",
            "aapl-10q-doc-real",
            "--document-revision",
            "2026-11-05-02",
            "--financial-dataset",
            "aapl-edgar-real",
            "--financial-revision",
            "2026-10-28-02",
            "--dataset",
            "aapl-research-mock",
            "--revision",
            "1",
            "--provider",
            "mock",
            "--accession",
            ACCESSION,
        ]
    )
    assert args.provider == "mock"
    assert args.accession == ACCESSION


def test_mock_research_is_deterministic_and_isolated(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    engine = ResearchEngine(store)
    first = engine.analyze(
        _query(),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
        output_dataset="research-out",
        output_revision="a",
    )
    second = engine.analyze(
        _query(),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
        output_dataset="research-out",
        output_revision="b",
    )
    assert first.artifact.artifact_id == second.artifact.artifact_id
    assert MOCK_BANNER in first.artifact.thesis
    assert first.validation.valid
    assert first.artifact.validation_status == "valid"
    assert first.artifact.simulated is True
    assert "target price" not in first.artifact.thesis.lower()
    assert first.pack.chunks
    loaded = store.load("research-out", "a")
    assert loaded.research()[0].artifact_id == first.artifact.artifact_id
    assert loaded.manifest.metadata["llm_called"] is False
    with pytest.raises(RevisionExistsError):
        engine.analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="research-out",
            output_revision="a",
        )


def test_research_cli_mock_analyze(edgar_env: None, tmp_path: Path, capsys) -> None:
    store = _snapshots(tmp_path)
    assert (
        main(
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
                "--dataset",
                "cli-research",
                "--revision",
                "1",
                "--provider",
                "mock",
                "--accession",
                ACCESSION,
                "--as-of",
                AS_OF.isoformat(),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["simulated"] is True
    assert payload["llm_called"] is False
    assert payload["validation_status"] == "valid"


def test_future_as_of_hides_evidence(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    result = ResearchEngine(store).analyze(
        _query(research_as_of=datetime(2020, 1, 1, tzinfo=UTC)),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
        output_dataset="research-out",
        output_revision="past",
    )
    assert result.pack.chunks == ()
    assert result.pack.facts == ()
    assert result.artifact.claims == ()
    assert result.validation.valid


def test_intraday_excludes_unverified(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    filings = store.load("edgar-aapl", "meta-1").filings()
    naive = next(item for item in filings if not item.acceptance_verified)
    pack = ResearchEngine(store).retriever.retrieve(
        _query(
            accession=naive.accession,
            intraday=True,
            research_as_of=datetime(2026, 12, 1, tzinfo=UTC),
        ),
        document_dataset="edgar-docs",
        document_revision="1",
        financial_dataset="edgar-aapl",
        financial_revision="meta-1",
    )
    assert pack.documents == ()
    assert pack.facts == ()


def test_oversized_question_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="max_question_chars"):
        ResearchQuery(symbol="AAPL", research_as_of=AS_OF, question="x" * 2001)


def test_openai_provider_is_disabled() -> None:
    with pytest.raises(NotImplementedError, match="M3-B"):
        provider_from_name("openai")
    with pytest.raises(NotImplementedError):
        OpenAIResearchProvider()


def test_validator_rejects_missing_and_bad_citations() -> None:
    query = _query()
    time = _time(1)
    excerpt = "Net sales were 94036 million in the quarter."
    chunk = FilingTextChunk(
        chunk_id=f"filing-text:{ACCESSION}:{'a' * 64}:0",
        accession=ACCESSION,
        document_sha256="a" * 64,
        source_url=CANONICAL,
        text=excerpt,
        text_start=0,
        text_end=len(excerpt),
        time=time,
        provenance=_prov(),
        section_heading="Item 2. MD&A",
    )
    document = FilingDocumentRecord(
        symbol="AAPL",
        accession=ACCESSION,
        cik="0000320193",
        form_type="10-Q",
        primary_document="aapl-20260627.htm",
        source_url=CANONICAL,
        sha256="a" * 64,
        mime_type="text/html",
        byte_length=10,
        time=time,
        provenance=_prov(),
        retrieved_at=time.available_time,
        encoding="utf-8",
        cleaned_text=excerpt,
        acceptance_verified=True,
        intraday_eligible=True,
    )
    fact = XbrlFactRecord(
        symbol="AAPL",
        cik="0000320193",
        concept="NetSales",
        taxonomy="us-gaap",
        unit="USD",
        value=1.0,
        time=time,
        provenance=_prov(),
        accession=ACCESSION,
        acceptance_verified=True,
        intraday_eligible=True,
    )
    pack = EvidencePack(
        query=query,
        documents=(document,),
        chunks=(chunk,),
        facts=(fact,),
        document_dataset="docs",
        document_revision="1",
        financial_dataset="fin",
        financial_revision="1",
    )
    validator = EvidenceValidator()
    missing = ResearchArtifact(
        artifact_id="r1",
        model_id="m",
        model_version="1",
        time=time,
        provenance=_prov(),
        symbols=("AAPL",),
        thesis="x" * 24,
        claims=(
            ResearchClaim(
                claim_id="c1",
                text="Sales rose.",
                category="performance",
            ),
        ),
    )
    report = validator.validate(missing, pack)
    assert not report.valid
    assert any(issue.code == "missing_citation" for issue in report.issues)

    bad_hash = ResearchArtifact(
        artifact_id="r2",
        model_id="m",
        model_version="1",
        time=time,
        provenance=_prov(),
        symbols=("AAPL",),
        thesis="x" * 24,
        evidence_refs=(f"filing-doc:{ACCESSION}:{'b' * 64}",),
        claims=(
            ResearchClaim(
                claim_id="c2",
                text="Sales rose.",
                category="performance",
                evidence_refs=(f"filing-doc:{ACCESSION}:{'b' * 64}",),
            ),
        ),
    )
    assert any(issue.code == "hash_mismatch" for issue in validator.validate(bad_hash, pack).issues)

    bad_off = ResearchArtifact(
        artifact_id="r3",
        model_id="m",
        model_version="1",
        time=time,
        provenance=_prov(),
        symbols=("AAPL",),
        thesis="x" * 24,
        claims=(
            ResearchClaim(
                claim_id="c3",
                text="Sales rose.",
                category="performance",
                evidence_refs=(chunk.chunk_id,),
                chunk_id=chunk.chunk_id,
                quoted_span="not in the filing",
                text_start=0,
                text_end=17,
            ),
        ),
    )
    assert any(issue.code == "quote_mismatch" for issue in validator.validate(bad_off, pack).issues)

    future_pack = EvidencePack(
        query=_query(research_as_of=datetime(2026, 7, 1, tzinfo=UTC)),
        documents=pack.documents,
        chunks=pack.chunks,
        facts=pack.facts,
        document_dataset="docs",
        document_revision="1",
        financial_dataset="fin",
        financial_revision="1",
    )
    future_art = ResearchArtifact(
        artifact_id="r4",
        model_id="m",
        model_version="1",
        time=time,
        provenance=_prov(),
        symbols=("AAPL",),
        thesis="x" * 24,
        evidence_refs=(chunk.chunk_id,),
        claims=(
            ResearchClaim(
                claim_id="c4",
                text=chunk.text,
                category="performance",
                evidence_refs=(chunk.chunk_id,),
                chunk_id=chunk.chunk_id,
                quoted_span=chunk.text,
                text_start=0,
                text_end=len(chunk.text),
            ),
        ),
    )
    assert any(
        issue.code == "future_evidence"
        for issue in validator.validate(future_art, future_pack).issues
    )


def test_empty_evidence_bounds_are_rejected() -> None:
    with pytest.raises(InvalidInputError, match="bounds must be positive"):
        ResearchQuery(symbol="AAPL", research_as_of=AS_OF, max_chunks=0)


def test_research_output_cannot_reuse_source_revision(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    with pytest.raises(InvalidInputError, match="new immutable revision"):
        ResearchEngine(store).analyze(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
            output_dataset="edgar-docs",
            output_revision="1",
        )


def test_corrupted_snapshot_fails_before_research(edgar_env: None, tmp_path: Path) -> None:
    store = _snapshots(tmp_path)
    target = next(
        path
        for path in (tmp_path / "edgar-docs" / "1").rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(SnapshotIntegrityError):
        SnapshotEvidenceRetriever(store).retrieve(
            _query(),
            document_dataset="edgar-docs",
            document_revision="1",
            financial_dataset="edgar-aapl",
            financial_revision="meta-1",
        )


REAL_SNAPSHOTS = Path(__file__).resolve().parents[1] / "data" / "snapshots"
REAL_DOCS = REAL_SNAPSHOTS / "aapl-10q-doc-real" / "2026-11-05-02" / "manifest.json"
REAL_FACTS = REAL_SNAPSHOTS / "aapl-edgar-real" / "2026-10-28-02" / "manifest.json"


@pytest.mark.skipif(
    not (REAL_DOCS.is_file() and REAL_FACTS.is_file()),
    reason="local Apple snapshots are not present",
)
def test_real_apple_snapshots_are_schema_compatible() -> None:
    store = SnapshotStore(REAL_SNAPSHOTS)
    store.verify("aapl-10q-doc-real", "2026-11-05-02")
    store.verify("aapl-edgar-real", "2026-10-28-02")
    docs = store.load("aapl-10q-doc-real", "2026-11-05-02")
    assert docs.filing_documents()
    assert docs.filing_text_chunks()
    as_of = max(item.time.available_time for item in docs.filing_documents())
    pack = SnapshotEvidenceRetriever(store).retrieve(
        ResearchQuery(symbol="AAPL", research_as_of=as_of, accession=ACCESSION),
        document_dataset="aapl-10q-doc-real",
        document_revision="2026-11-05-02",
        financial_dataset="aapl-edgar-real",
        financial_revision="2026-10-28-02",
    )
    assert pack.documents
    assert pack.chunks
    assert pack.untrusted is True


def test_research_package_does_not_import_trading() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "atlas" / "research"
    forbidden = ("atlas.paper_trading", "atlas.risk", "PaperBroker", "RiskManager")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} contains {token}"
