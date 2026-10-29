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
    RETRIEVAL_VENDOR_GET,
    FilingDocumentError,
    FilingDocumentQuery,
    adapter_for_tests,
    archive_document_url,
    validate_archive_url,
)
from atlas.ingest.filing_html import CONTENT_TRUST, cited_cleaned_text, extract_filing_text
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
    assert not extracted.cleaned_text.startswith("UNTRUSTED_SEC_DOCUMENT")
    assert extracted.content_trust == CONTENT_TRUST
    assert "window.alert" not in extracted.cleaned_text
    assert "94036" in extracted.cleaned_text
    assert "1.57" in extracted.cleaned_text
    assert any(
        chunk.section_heading and "Item 2" in chunk.section_heading for chunk in extracted.chunks
    )
    assert all(chunk.offset_kind in {"cleaned_text", "raw_bytes"} for chunk in extracted.chunks)
    for chunk in extracted.chunks:
        assert cited_cleaned_text(extracted.cleaned_text, chunk.text_start, chunk.text_end) == (
            chunk.text
        )
        assert "UNTRUSTED_SEC_DOCUMENT" not in chunk.text
    invented = [chunk for chunk in extracted.chunks if chunk.offset_kind == "raw_bytes"]
    raw = HTML.encode("utf-8")
    for chunk in invented:
        assert chunk.raw_byte_start is not None
        assert raw[chunk.raw_byte_start : chunk.raw_byte_end] == chunk.text.encode("utf-8")


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
    assert doc.content_trust == CONTENT_TRUST
    assert doc.byte_length == len(HTML.encode("utf-8"))
    assert not doc.cleaned_text.startswith("UNTRUSTED_SEC_DOCUMENT")
    matching = [chunk for chunk in result.chunks if chunk.accession == doc.accession]
    assert matching
    for chunk in matching:
        assert chunk.untrusted is True
        assert cited_cleaned_text(doc.cleaned_text, chunk.text_start, chunk.text_end) == chunk.text
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
    assert empty_result.documents == ()
    assert empty_result.manifest.metadata["incomplete_documents"]


def test_explicit_accession_does_not_substitute(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)
    recorded = {CANONICAL: {"status": 200, "headers": {"content-type": "text/html"}, "body": HTML}}
    result = adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
        store,
        source,
        dataset_id="edgar-docs",
        revision="picked",
        recorded=recorded,
        query=FilingDocumentQuery(accession="0000320193-26-000020", require_success=True),
    )
    assert [item.accession for item in result.documents] == ["0000320193-26-000020"]
    assert result.documents[0].form_type == "10-Q"
    assert result.documents[0].primary_document == "aapl-20260627.htm"
    with pytest.raises(FilingDocumentError, match="was not found"):
        adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="missing",
            recorded=recorded,
            query=FilingDocumentQuery(accession="0000320193-99-999999", require_success=True),
        )
    assert not store.revision_path("edgar-docs", "missing").exists()
    with pytest.raises(FilingDocumentError, match="was not retrieved"):
        adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="wrong-url",
            recorded=recorded,
            query=FilingDocumentQuery(accession="0000320193-24-000200", require_success=True),
        )
    assert not store.revision_path("edgar-docs", "wrong-url").exists()


def test_failed_single_fetch_is_not_success(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)
    with pytest.raises(FilingDocumentError, match="no usable documents"):
        adapter_for_tests(lambda r: httpx.Response(500)).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="fail-once",
            recorded={
                CANONICAL: {"status": 403, "headers": {"content-type": "text/html"}, "body": ""}
            },
            query=FilingDocumentQuery(max_documents=1, require_success=True),
        )
    assert not store.revision_path("edgar-docs", "fail-once").exists()


def test_vendor_get_does_not_retain_oversize_body(edgar_env: None, tmp_path: Path) -> None:
    _source(tmp_path)
    source = SnapshotStore(tmp_path).load("edgar-aapl", "meta-1")
    store = SnapshotStore(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 50_000,
            headers={"content-type": "text/html", "content-length": "50000"},
        )

    with pytest.raises(FilingDocumentError, match="max_bytes"):
        adapter_for_tests(handler).ingest_from_source(
            store,
            source,
            dataset_id="edgar-docs",
            revision="bounded",
            query=FilingDocumentQuery(
                max_documents=1,
                max_bytes=32,
                accession="0000320193-26-000020",
                require_success=True,
            ),
            retrieval_mode=RETRIEVAL_VENDOR_GET,
        )
    assert not store.revision_path("edgar-docs", "bounded").exists()


def test_citation_offsets_for_entities_tables_repeats_unicode_and_long_sections() -> None:
    html = f"""
    <html><body>
    <h2>Item 1. Notes</h2>
    <p>Revenue was &#36;1.57 and &amp; equity was 70&nbsp;billion.</p>
    <table><tr><td>Cash</td><td>€94,036</td></tr><tr><td>매출</td><td>₩1,000</td></tr></table>
    <p>Same sentence appears twice. Same sentence appears twice.</p>
    <p>{"Long section " * 400}</p>
    </body></html>
    """
    raw = html.encode("utf-8")
    extracted = extract_filing_text(raw, mime_type="text/html", filename="notes.htm")
    assert "$1.57" in extracted.cleaned_text
    assert "&amp;" not in extracted.cleaned_text
    assert "€94,036" in extracted.cleaned_text
    assert "₩1,000" in extracted.cleaned_text
    assert "| Cash | €94,036 |" in extracted.cleaned_text
    assert extracted.cleaned_text.count("Same sentence appears twice.") == 2
    assert len(extracted.chunks) >= 2
    for chunk in extracted.chunks:
        cited = cited_cleaned_text(extracted.cleaned_text, chunk.text_start, chunk.text_end)
        assert cited == chunk.text
        if chunk.offset_kind == "raw_bytes":
            assert raw[chunk.raw_byte_start : chunk.raw_byte_end] == chunk.text.encode("utf-8")
            assert raw.count(chunk.text.encode("utf-8")) == 1
        else:
            assert chunk.raw_byte_start is None
    repeats = [chunk for chunk in extracted.chunks if "Same sentence appears twice." in chunk.text]
    assert repeats
    assert all(chunk.offset_kind == "cleaned_text" for chunk in repeats)


def test_plain_text_raw_byte_offsets_are_independently_verified() -> None:
    body = "Item 8.01 unique exhibit sentence about cash of 1234567890.\n"
    extracted = extract_filing_text(body.encode("utf-8"), mime_type="text/plain", filename="a.txt")
    assert extracted.chunks
    chunk = extracted.chunks[0]
    assert chunk.offset_kind == "raw_bytes"
    assert body.encode("utf-8")[chunk.raw_byte_start : chunk.raw_byte_end] == chunk.text.encode(
        "utf-8"
    )
    assert (
        cited_cleaned_text(extracted.cleaned_text, chunk.text_start, chunk.text_end) == chunk.text
    )
