"""Point-in-time research evidence retrieval from immutable snapshots. No HTTP."""

from __future__ import annotations

import re
from datetime import datetime

from atlas.contracts import FilingDocumentRecord, FilingTextChunk, XbrlFactRecord
from atlas.ingest.snapshot import SnapshotSource, SnapshotStore
from atlas.research.models import PREFERRED_SECTIONS, EvidencePack, ResearchQuery
from atlas.validation import InvalidInputError

_TOKEN_RE = re.compile(r"[a-z0-9$%]+")
_FACT_PRIORITY = (
    "revenuefromcontractwithcustomer",
    "revenues",
    "netincomeloss",
    "operatingincome",
    "earningspersharediluted",
    "earningspersharebasic",
)


class SnapshotEvidenceRetriever:
    """Load verified snapshot evidence. Never fabricates text or values."""

    def __init__(self, store: SnapshotStore) -> None:
        self.store = store

    def retrieve(
        self,
        query: ResearchQuery,
        *,
        document_dataset: str,
        document_revision: str,
        financial_dataset: str,
        financial_revision: str,
    ) -> EvidencePack:
        documents_src = self._load_verified(document_dataset, document_revision)
        financial_src = self._load_verified(financial_dataset, financial_revision)
        self._assert_hashes(documents_src)
        as_of = query.research_as_of
        documents = tuple(
            item
            for item in documents_src.filing_documents()
            if _visible(item.time.available_time, as_of)
            and _symbol_ok(item.symbol, query.symbol)
            and _accession_ok(item.accession, query.accession)
            and _intraday_ok(item.acceptance_verified, item.intraday_eligible, query)
        )
        chunks = tuple(
            item
            for item in documents_src.filing_text_chunks()
            if _visible(item.time.available_time, as_of)
            and _accession_ok(item.accession, query.accession)
            and any(doc.accession == item.accession for doc in documents)
            and _chunk_hash_ok(item, documents)
        )
        facts = tuple(
            item
            for item in financial_src.facts()
            if _visible(item.time.available_time, as_of)
            and _symbol_ok(item.symbol, query.symbol)
            and _accession_ok(item.accession, query.accession)
            and _intraday_ok(item.acceptance_verified, item.intraday_eligible, query)
        )
        ranked_chunks = _rank_chunks(chunks, query)[: query.max_chunks]
        ranked_facts = _rank_facts(facts, query)[: query.max_facts]
        return EvidencePack(
            query=query,
            documents=documents,
            chunks=ranked_chunks,
            facts=ranked_facts,
            document_dataset=document_dataset,
            document_revision=document_revision,
            financial_dataset=financial_dataset,
            financial_revision=financial_revision,
        )

    def _load_verified(self, dataset_id: str, revision: str) -> SnapshotSource:
        self.store.verify(dataset_id, revision)
        return self.store.load(dataset_id, revision)

    def _assert_hashes(self, source: SnapshotSource) -> None:
        by_accn = {item.accession: item for item in source.filing_documents()}
        for chunk in source.filing_text_chunks():
            document = by_accn.get(chunk.accession)
            if document is None:
                continue
            if chunk.document_sha256 != document.sha256:
                raise InvalidInputError(
                    f"chunk {chunk.chunk_id} document hash does not match stored filing bytes."
                )


def _visible(available: datetime, as_of: datetime) -> bool:
    return available <= as_of


def _symbol_ok(symbol: str, wanted: str) -> bool:
    return symbol == wanted


def _accession_ok(accession: str, wanted: str | None) -> bool:
    return wanted is None or accession == wanted


def _intraday_ok(verified: bool, eligible: bool, query: ResearchQuery) -> bool:
    if query.allow_unverified and not query.intraday:
        return True
    if query.intraday:
        return bool(verified and eligible)
    return bool(verified)


def _chunk_hash_ok(chunk: FilingTextChunk, documents: tuple[FilingDocumentRecord, ...]) -> bool:
    for document in documents:
        if document.accession == chunk.accession:
            return chunk.document_sha256 == document.sha256
    return False


def _rank_chunks(
    chunks: tuple[FilingTextChunk, ...], query: ResearchQuery
) -> tuple[FilingTextChunk, ...]:
    tokens = set(_TOKEN_RE.findall(query.question.lower()))
    scored: list[tuple[int, str, str, FilingTextChunk]] = []
    for chunk in chunks:
        heading = (chunk.section_heading or "").lower()
        text = chunk.text.lower()
        score = 0
        if any(marker in heading for marker in PREFERRED_SECTIONS):
            score += 50
        score += sum(2 for token in tokens if token in heading)
        score += sum(1 for token in tokens if token in text)
        scored.append((-score, chunk.accession, chunk.chunk_id, chunk))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in scored)


def _rank_facts(
    facts: tuple[XbrlFactRecord, ...], query: ResearchQuery
) -> tuple[XbrlFactRecord, ...]:
    scored: list[tuple[int, str, str, XbrlFactRecord]] = []
    tokens = set(_TOKEN_RE.findall(query.question.lower()))
    for fact in facts:
        concept = fact.concept.lower()
        score = 0
        for index, name in enumerate(_FACT_PRIORITY):
            if name in concept:
                score += 40 - index
                break
        score += sum(1 for token in tokens if token in concept)
        scored.append((-score, fact.accession, fact.ref_id, fact))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in scored)
