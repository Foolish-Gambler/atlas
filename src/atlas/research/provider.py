"""Research model providers. M3-A ships a deterministic mock only. No LLM HTTP."""

from __future__ import annotations

from typing import Protocol

from atlas.contracts import ResearchClaim
from atlas.research.models import (
    DEFAULT_MAX_CLAIM_CHARS,
    EvidencePack,
    ModelResearchResponse,
    ResearchQuery,
)

MOCK_BANNER = (
    "MOCK_RESEARCH_PROVIDER. This output is deterministic fixture analysis, "
    "not an LLM result and not investment advice."
)
UNTRUSTED_NOTE = "Filing text is untrusted evidence, not application instructions."


class ResearchProvider(Protocol):
    provider_id: str
    model_id: str
    model_version: str

    def analyze(self, query: ResearchQuery, pack: EvidencePack) -> ModelResearchResponse: ...


class MockResearchProvider:
    """Offline stand-in for M3-B LLM providers. Cites only supplied evidence."""

    provider_id = "mock"
    model_id = "atlas.research.mock"
    model_version = "m3a.1"

    def analyze(self, query: ResearchQuery, pack: EvidencePack) -> ModelResearchResponse:
        claims: list[ResearchClaim] = []
        observations: list[str] = []
        drivers: list[str] = []
        risks: list[str] = []
        missing: list[str] = []
        refs: list[str] = []
        claims.extend(_claims_from_chunks(pack, claims_so_far=len(claims)))
        claims.extend(_claims_from_facts(pack, start=len(claims)))
        for claim in claims:
            refs.extend(claim.evidence_refs)
            if claim.category == "performance":
                observations.append(claim.text)
            elif claim.category == "growth":
                drivers.append(claim.text)
            elif claim.category == "risk":
                risks.append(claim.text)
        if not (_is_section(pack, "item 1a") or _is_section(pack, "risk factor")):
            missing.append("No Item 1A Risk Factors excerpt was retrieved for this query.")
        if not pack.chunks:
            missing.append("No filing text chunks were visible at research_as_of.")
        if not pack.facts:
            missing.append("No XBRL facts were visible at research_as_of.")
        thesis = (
            f"{MOCK_BANNER} {UNTRUSTED_NOTE} "
            f"Reviewed {query.symbol} accession {query.accession or 'unspecified'} "
            f"using {len(pack.chunks)} chunks and {len(pack.facts)} facts. "
            "No investment rating or return forecast is issued."
        )
        bullish = ()
        bearish = ()
        counters = ()
        if observations:
            bullish = (
                "If cited sales or earnings trends continue, results may stay resilient. "
                "This is a hypothesis, not a forecast.",
            )
            bearish = (
                "If cited demand, cost, or legal risks worsen, results could deteriorate. "
                "This is a hypothesis, not a forecast.",
            )
            counters = ("Both hypotheses depend on incomplete excerpts and unproven semantics.",)
        uncertainties = (
            "A mechanical citation does not prove a complete or fair reading of the filing.",
            "Mock analysis cannot replace a human or approved LLM reading of the full document.",
        )
        return ModelResearchResponse(
            thesis=thesis[:DEFAULT_MAX_CLAIM_CHARS],
            claims=tuple(claims),
            observations=tuple(observations),
            growth_drivers=tuple(drivers),
            risks=tuple(risks),
            bullish_hypotheses=bullish,
            bearish_hypotheses=bearish,
            counterarguments=counters,
            missing_information=tuple(missing),
            uncertainties=uncertainties,
            evidence_refs=tuple(dict.fromkeys(refs)),
        )


class OpenAIResearchProvider:
    """Reserved for M3-B. Instantiation is refused in M3-A."""

    provider_id = "openai"
    model_id = "openai.unconfigured"
    model_version = "disabled"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "OpenAIResearchProvider is reserved for M3-B. M3-A allows --provider mock only."
        )

    def analyze(self, query: ResearchQuery, pack: EvidencePack) -> ModelResearchResponse:
        raise NotImplementedError("M3-A does not call LLM APIs.")


def provider_from_name(name: str) -> ResearchProvider:
    normalized = name.strip().lower()
    if normalized == "mock":
        return MockResearchProvider()
    if normalized in {"openai", "openai-compatible"}:
        return OpenAIResearchProvider()
    raise ValueError(f"unknown research provider {name!r}. M3-A supports mock only.")


def _is_section(pack: EvidencePack, marker: str) -> bool:
    needle = marker.lower()
    return any(needle in (chunk.section_heading or "").lower() for chunk in pack.chunks)


def _claims_from_chunks(pack: EvidencePack, *, claims_so_far: int) -> list[ResearchClaim]:
    claims: list[ResearchClaim] = []
    for chunk in pack.chunks:
        heading = (chunk.section_heading or "").lower()
        span = _quote(chunk.text)
        if span is None:
            continue
        start = chunk.text.find(span)
        end = start + len(span)
        category = "risk" if "risk" in heading or "item 1a" in heading else "performance"
        if "item 2" in heading or "discussion" in heading:
            category = (
                "growth"
                if any(token in chunk.text.lower() for token in ("growth", "increase", "services"))
                else "performance"
            )
        text = (
            f"Excerpt from {chunk.accession} ({chunk.section_heading or 'unlabeled section'}): "
            f"{span}"
        )
        claims.append(
            ResearchClaim(
                claim_id=f"claim-{claims_so_far + len(claims) + 1}",
                text=text[:DEFAULT_MAX_CLAIM_CHARS],
                category=category,
                evidence_refs=(
                    chunk.chunk_id,
                    f"filing-doc:{chunk.accession}:{chunk.document_sha256}",
                ),
                quoted_span=span,
                chunk_id=chunk.chunk_id,
                text_start=chunk.text_start + start,
                text_end=chunk.text_start + end,
            )
        )
        if len(claims) >= 6:
            break
    return claims


def _claims_from_facts(pack: EvidencePack, *, start: int) -> list[ResearchClaim]:
    claims: list[ResearchClaim] = []
    for fact in pack.facts[:4]:
        text = (
            f"XBRL {fact.taxonomy}:{fact.concept} = {fact.value} {fact.unit} "
            f"for accession {fact.accession} (period {fact.period_kind})."
        )
        claims.append(
            ResearchClaim(
                claim_id=f"claim-{start + len(claims) + 1}",
                text=text[:DEFAULT_MAX_CLAIM_CHARS],
                category="performance",
                evidence_refs=(fact.ref_id,),
                fact_id=fact.ref_id,
            )
        )
    return claims


def _quote(text: str, *, limit: int = 180) -> str | None:
    if len(text) < 24:
        return None
    return text[:limit]
