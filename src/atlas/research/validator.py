"""Deterministic mechanical citation checks. Not semantic correctness."""

from __future__ import annotations

from atlas.contracts import FilingTextChunk, ResearchArtifact, ResearchClaim, XbrlFactRecord
from atlas.research.models import EvidencePack, ValidationIssue, ValidationReport

_FACTUAL_CATEGORIES = frozenset({"performance", "growth", "risk"})


class EvidenceValidator:
    def validate(self, artifact: ResearchArtifact, pack: EvidencePack) -> ValidationReport:
        issues: list[ValidationIssue] = []
        as_of = pack.query.research_as_of
        chunks = pack.chunk_by_id()
        facts = pack.fact_by_id()
        documents = pack.document_by_accession()
        for ref in artifact.evidence_refs:
            issues.extend(_check_ref(ref, pack, chunks, facts, documents, as_of, claim_id=None))
        for claim in artifact.claims:
            issues.extend(_check_claim(claim, pack, chunks, facts, documents, as_of))
        unique: list[ValidationIssue] = []
        seen: set[tuple[str, str, str | None]] = set()
        for issue in issues:
            key = (issue.code, issue.message, issue.claim_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        return ValidationReport(
            issues=tuple(unique),
            valid=not unique,
            mechanically_checked=True,
            semantically_verified=False,
        )


def _check_claim(
    claim: ResearchClaim,
    pack: EvidencePack,
    chunks: dict[str, FilingTextChunk],
    facts: dict[str, XbrlFactRecord],
    documents: dict,
    as_of,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if claim.category in _FACTUAL_CATEGORIES and not claim.evidence_refs:
        issues.append(
            ValidationIssue(
                "missing_citation",
                "factual claim has no evidence_refs.",
                claim_id=claim.claim_id,
            )
        )
        return issues
    for ref in claim.evidence_refs:
        issues.extend(
            _check_ref(ref, pack, chunks, facts, documents, as_of, claim_id=claim.claim_id)
        )
    if claim.chunk_id:
        chunk = chunks.get(claim.chunk_id)
        if chunk is None:
            issues.append(
                ValidationIssue(
                    "missing_chunk",
                    f"chunk_id {claim.chunk_id} is not in retrieved evidence.",
                    ref_id=claim.chunk_id,
                    claim_id=claim.claim_id,
                )
            )
        else:
            issues.extend(_check_offsets(claim, chunk, pack))
    if claim.fact_id:
        fact = facts.get(claim.fact_id)
        if fact is None:
            issues.append(
                ValidationIssue(
                    "missing_fact",
                    f"fact_id {claim.fact_id} is not in retrieved evidence.",
                    ref_id=claim.fact_id,
                    claim_id=claim.claim_id,
                )
            )
        elif not fact.unit.strip():
            issues.append(
                ValidationIssue(
                    "invalid_unit",
                    "XBRL fact is missing a unit.",
                    ref_id=fact.ref_id,
                    claim_id=claim.claim_id,
                )
            )
    if claim.semantically_verified:
        issues.append(
            ValidationIssue(
                "semantic_overclaim",
                "M3-A cannot mark citations as semantically verified.",
                claim_id=claim.claim_id,
            )
        )
    return issues


def _check_ref(
    ref: str,
    pack: EvidencePack,
    chunks: dict[str, FilingTextChunk],
    facts: dict[str, XbrlFactRecord],
    documents: dict,
    as_of,
    *,
    claim_id: str | None,
) -> list[ValidationIssue]:
    if ref.startswith("filing-text:") or ref in chunks:
        chunk = chunks.get(ref)
        if chunk is None:
            return [
                ValidationIssue(
                    "missing_chunk",
                    f"evidence ref {ref} is not a retrieved chunk.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        return _check_chunk_visibility(chunk, pack, documents, as_of, claim_id)
    if ref.startswith("filing-doc:"):
        parts = ref.split(":")
        if len(parts) != 3:
            return [
                ValidationIssue(
                    "malformed_ref",
                    f"document ref {ref} is malformed.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        accession, digest = parts[1], parts[2]
        document = documents.get(accession)
        if document is None:
            return [
                ValidationIssue(
                    "missing_document",
                    f"document {accession} is not in retrieved evidence.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        if document.sha256 != digest:
            return [
                ValidationIssue(
                    "hash_mismatch",
                    "document hash in citation does not match stored bytes.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        if document.time.available_time > as_of:
            return [
                ValidationIssue(
                    "future_evidence",
                    "document was not available at research_as_of.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        return []
    if ref.startswith("fact:") or ref in facts:
        fact = facts.get(ref)
        if fact is None:
            return [
                ValidationIssue(
                    "missing_fact",
                    f"evidence ref {ref} is not a retrieved XBRL fact.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        if fact.time.available_time > as_of:
            return [
                ValidationIssue(
                    "future_evidence",
                    "XBRL fact was not available at research_as_of.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        if pack.query.intraday and not fact.intraday_eligible:
            return [
                ValidationIssue(
                    "intraday_restricted",
                    "unverified or ineligible fact cannot be used for intraday research.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        if not fact.unit.strip():
            return [
                ValidationIssue(
                    "invalid_unit",
                    "XBRL fact is missing a unit.",
                    ref_id=ref,
                    claim_id=claim_id,
                )
            ]
        return []
    return [
        ValidationIssue(
            "unknown_ref",
            f"evidence ref {ref} does not match a chunk, document, or fact.",
            ref_id=ref,
            claim_id=claim_id,
        )
    ]


def _check_chunk_visibility(
    chunk: FilingTextChunk,
    pack: EvidencePack,
    documents: dict,
    as_of,
    claim_id: str | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if chunk.time.available_time > as_of:
        issues.append(
            ValidationIssue(
                "future_evidence",
                "chunk was not available at research_as_of.",
                ref_id=chunk.chunk_id,
                claim_id=claim_id,
            )
        )
    document = documents.get(chunk.accession)
    if document is None:
        issues.append(
            ValidationIssue(
                "missing_document",
                f"chunk accession {chunk.accession} has no retrieved document.",
                ref_id=chunk.chunk_id,
                claim_id=claim_id,
            )
        )
    elif chunk.document_sha256 != document.sha256:
        issues.append(
            ValidationIssue(
                "hash_mismatch",
                "chunk document hash does not match stored filing bytes.",
                ref_id=chunk.chunk_id,
                claim_id=claim_id,
            )
        )
    if pack.query.intraday and document is not None and not document.intraday_eligible:
        issues.append(
            ValidationIssue(
                "intraday_restricted",
                "unverified filing cannot be used for intraday research.",
                ref_id=chunk.chunk_id,
                claim_id=claim_id,
            )
        )
    return issues


def _check_offsets(
    claim: ResearchClaim, chunk: FilingTextChunk, pack: EvidencePack
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    document = pack.document_by_accession().get(chunk.accession)
    cleaned = document.cleaned_text if document is not None else chunk.text
    if claim.text_start is None or claim.text_end is None:
        if claim.quoted_span and claim.quoted_span not in chunk.text:
            issues.append(
                ValidationIssue(
                    "quote_mismatch",
                    "quoted_span is not present in the cited chunk text.",
                    ref_id=chunk.chunk_id,
                    claim_id=claim.claim_id,
                )
            )
        return issues
    if claim.text_start < 0 or claim.text_end < claim.text_start:
        issues.append(
            ValidationIssue(
                "bad_offsets",
                "citation offsets are unordered.",
                ref_id=chunk.chunk_id,
                claim_id=claim.claim_id,
            )
        )
        return issues
    if document is not None:
        if claim.text_end > len(cleaned):
            issues.append(
                ValidationIssue(
                    "bad_offsets",
                    "citation offsets exceed cleaned_text.",
                    ref_id=chunk.chunk_id,
                    claim_id=claim.claim_id,
                )
            )
            return issues
        cited = cleaned[claim.text_start : claim.text_end]
        if claim.quoted_span is not None and cited != claim.quoted_span:
            issues.append(
                ValidationIssue(
                    "quote_mismatch",
                    "quoted_span does not equal cleaned_text[text_start:text_end].",
                    ref_id=chunk.chunk_id,
                    claim_id=claim.claim_id,
                )
            )
    elif claim.quoted_span and claim.quoted_span not in chunk.text:
        issues.append(
            ValidationIssue(
                "quote_mismatch",
                "quoted_span is not present in the cited chunk text.",
                ref_id=chunk.chunk_id,
                claim_id=claim.claim_id,
            )
        )
    return issues
