"""Structured research request, evidence, and validation types. No LLM network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from atlas.contracts import FilingDocumentRecord, FilingTextChunk, ResearchClaim, XbrlFactRecord
from atlas.validation import InvalidInputError, normalize_symbol, require_aware_datetime

DEFAULT_QUESTION = (
    "What changed in revenue, profitability, growth drivers, and material risks "
    "in this filing? Cite only provided evidence."
)
PREFERRED_SECTIONS = (
    "item 2",
    "management's discussion",
    "item 1a",
    "risk factor",
)
MAX_QUESTION_CHARS = 2_000
DEFAULT_MAX_CHUNKS = 16
DEFAULT_MAX_FACTS = 24
DEFAULT_MAX_CLAIM_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class ResearchQuery:
    symbol: str
    research_as_of: datetime
    question: str = DEFAULT_QUESTION
    accession: str | None = None
    max_chunks: int = DEFAULT_MAX_CHUNKS
    max_facts: int = DEFAULT_MAX_FACTS
    max_question_chars: int = MAX_QUESTION_CHARS
    intraday: bool = False
    allow_unverified: bool = False
    retrospective: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(
            self, "research_as_of", require_aware_datetime(self.research_as_of, "research_as_of")
        )
        question = self.question.strip() or DEFAULT_QUESTION
        if len(question) > self.max_question_chars:
            raise InvalidInputError("research question exceeds max_question_chars.")
        object.__setattr__(self, "question", question)
        if self.max_chunks < 1 or self.max_facts < 1:
            raise InvalidInputError("research evidence bounds must be positive.")
        if self.accession == "":
            object.__setattr__(self, "accession", None)


@dataclass(frozen=True, slots=True)
class EvidencePack:
    query: ResearchQuery
    documents: tuple[FilingDocumentRecord, ...]
    chunks: tuple[FilingTextChunk, ...]
    facts: tuple[XbrlFactRecord, ...]
    document_dataset: str
    document_revision: str
    financial_dataset: str
    financial_revision: str
    untrusted: bool = True

    def chunk_by_id(self) -> dict[str, FilingTextChunk]:
        return {item.chunk_id: item for item in self.chunks}

    def fact_by_id(self) -> dict[str, XbrlFactRecord]:
        return {item.ref_id: item for item in self.facts}

    def document_by_accession(self) -> dict[str, FilingDocumentRecord]:
        return {item.accession: item for item in self.documents}


@dataclass(frozen=True, slots=True)
class ModelResearchResponse:
    thesis: str
    claims: tuple[ResearchClaim, ...]
    observations: tuple[str, ...] = ()
    growth_drivers: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    bullish_hypotheses: tuple[str, ...] = ()
    bearish_hypotheses: tuple[str, ...] = ()
    counterarguments: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    llm_called: bool = False
    simulated: bool = True
    prompt_version: str = "m3a.mock"
    schema_version: str = "m3a.research.v1"
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_count: int = 0
    generated_at: datetime | None = None
    estimated_cost: float | None = None
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    ref_id: str | None = None
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    valid: bool
    mechanically_checked: bool = True
    semantically_verified: bool = False
