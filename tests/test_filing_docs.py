from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from atlas.backtesting.view import build_view
from atlas.contracts import EventClock
from atlas.ingest.edgar import EdgarQuery
from atlas.ingest.edgar import adapter_for_tests as edgar_adapter
from atlas.ingest.filing_docs import (
    FilingDocumentError,
    FilingDocumentQuery,
    adapter_for_tests,
    archive_document_url,
    validate_archive_url,
)
from atlas.ingest.filing_html import UNTRUSTED_PREFIX, extract_filing_text
from atlas.ingest.snapshot import RevisionExistsError, SnapshotStore
from atlas.validation import InvalidInputError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ingest" / "edgar"
HTML = (FIXTURE_DIR / "aapl_10q_excerpt.html").read_text(encoding="utf-8")
CANONICAL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"


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


def _source(tmp_path: Path):
    return edgar_adapter(_edgar_handler()).ingest(
        SnapshotStore(tmp_path),
        dataset_id="edgar-aapl",
        revision="meta-1",
        query=EdgarQuery(symbol="AAPL"),
    )


def test_archive_url_rejects_traversal_and_foreign_hosts() -> None:
    url = archive_document_url("0000320193", "0000320193-26-000020", "aapl-20260627.htm")
    assert url == CANONICAL
    validate_archive_url(
        url,
        cik="0000320193",
        accession="0000320193-26-000020",
        primary_document="aapl-20260627.htm",
    )
    with pytest.raises(FilingDocumentError):
        archive_document_url("320193", "0000320193-26-000020", "../secret.htm")
    with pytest.raises(FilingDocumentError):
        archive_document_url("320193", "0000320193-26-000020", "notes.exe")
    with pytest.raises(FilingDocumentError):
        validate_archive_url(
            "https://evil.example/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm",
            cik="0000320193",
            accession="0000320193-26-000020",
            primary_document="aapl-20260627.htm",
        )
    with pytest.raises(FilingDocumentError):
        validate_archive_url(
            CANONICAL + "?token=1",
            cik="0000320193",
            accession="0000320193-26-000020",
            primary_document="aapl-20260627.htm",
        )


def test_html_extraction_keeps_tables_and_untrusted_banner() -> None:
    extracted = extract_filing_text(HTML.encode("utf-8"), mime_type="text/html", filename="a.htm")
    assert extracted.cleaned_text.startswith(UNTRUSTED_PREFIX)
    assert "window.alert" not in extracted.cleaned_text
    assert "94036" in extracted.cleaned_text
    assert "1.57" in extracted.cleaned_text
    assert any(
        chunk.section_heading and "Item 2" in chunk.section_heading for chunk in extracted.chunks
    )
    assert all(chunk.offset_kind in {"cleaned_text", "raw_bytes"} for chunk in extracted.chunks)
    invented = [chunk for chunk in extracted.chunks if chunk.offset_kind == "raw_bytes"]
    for chunk in invented:
        assert chunk.raw_byte_start is not None
        assert HTML.encode("utf-8")[chunk.raw_byte_start : chunk.raw_byte_end]


def test_recorded_document_ingest_and_pit(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    recorded = {CANONICAL: {"status": 200, "headers": {"content-type": "text/html"}, "body": HTML}}
    store = SnapshotStore(tmp_path)
    result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        store,
        source,
        dataset_id="edgar-docs",
        revision="1",
        query=FilingDocumentQuery(max_documents=3),
        recorded=recorded,
    )
    assert result.documents
    doc = next(item for item in result.documents if item.accession.endswith("000020"))
    assert doc.source_url == CANONICAL
    assert doc.sha256
    assert doc.untrusted if hasattr(doc, "untrusted") else True
    assert doc.byte_length == len(HTML.encode("utf-8"))
    assert all(
        "UNTRUSTED_SEC_DOCUMENT" in chunk.text
        for chunk in result.chunks
        if chunk.accession == doc.accession
    )
    loaded = store.load("edgar-docs", "1")
    assert loaded.manifest.files[f"filing-docs/{doc.accession}/aapl-20260627.htm"] == doc.sha256
    assert loaded.filing_document_bytes(doc.ref_id) == HTML.encode("utf-8")
    assert loaded.manifest.metadata["content_trust"] == "untrusted_sec_document"
    clock = EventClock(loaded.stream())
    early = datetime(2026, 7, 1, tzinfo=UTC)
    hidden = build_view(loaded, clock.visible(early), early)
    assert not hidden.filing_documents
    later = datetime(2026, 8, 2, tzinfo=UTC)
    visible = build_view(loaded, clock.visible(later), later)
    assert any(item.accession.endswith("000020") for item in visible.filing_documents)
    with pytest.raises(InvalidInputError, match="new immutable revision"):
        adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-aapl",
            revision="meta-1",
            recorded=recorded,
        )
    with pytest.raises(RevisionExistsError):
        adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="1",
            recorded=recorded,
        )


def test_unverified_filing_document_is_not_intraday(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    naive = next(item for item in source.filings() if item.accession.endswith("000200"))
    assert naive.acceptance_verified is False
    url = archive_document_url(naive.cik or "", naive.accession, naive.primary_document or "")
    recorded = {
        url: {"status": 200, "headers": {"content-type": "text/plain"}, "body": "plain 8-K"}
    }
    result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        SnapshotStore(tmp_path),
        source,
        dataset_id="edgar-docs",
        revision="naive",
        recorded=recorded,
        query=FilingDocumentQuery(max_documents=20),
    )
    doc = next(item for item in result.documents if item.accession.endswith("000200"))
    assert doc.acceptance_verified is False
    assert doc.intraday_eligible is False


def test_http_errors_and_unsafe_responses(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)
    for status, revision in ((403, "e403"), (429, "e429"), (302, "e302")):
        recorded = {
            CANONICAL: {
                "status": status,
                "headers": {"location": "https://example.com/x"},
                "body": "",
            }
        }
        result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision=revision,
            recorded=recorded,
            query=FilingDocumentQuery(max_documents=1),
        )
        assert result.documents == ()
        assert result.manifest.metadata["incomplete_documents"]
    huge = {CANONICAL: {"status": 200, "headers": {"content-type": "text/html"}, "body": "x" * 50}}
    result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        store,
        source,
        dataset_id="edgar-docs",
        revision="oversize",
        recorded=huge,
        query=FilingDocumentQuery(max_documents=1, max_bytes=10),
    )
    assert result.documents == ()
    empty = {CANONICAL: {"status": 200, "headers": {"content-type": "text/html"}, "body": ""}}
    empty_result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        store,
        source,
        dataset_id="edgar-docs",
        revision="empty",
        recorded=empty,
        query=FilingDocumentQuery(max_documents=1),
    )
    assert empty_result.documents[0].empty is True
