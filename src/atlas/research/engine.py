"""Offline research pipeline: retrieve, mock-analyze, validate, snapshot. No orders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from atlas.contracts import Provenance, ResearchArtifact, SourceKind, TimeIndex
from atlas.ingest.snapshot import SnapshotPayload, SnapshotStore
from atlas.research.models import EvidencePack, ResearchQuery, ValidationReport
from atlas.research.provider import ResearchProvider, provider_from_name
from atlas.research.retriever import SnapshotEvidenceRetriever
from atlas.research.validator import EvidenceValidator
from atlas.validation import InvalidInputError, normalize_symbol


@dataclass(frozen=True, slots=True)
class ResearchRunResult:
    artifact: ResearchArtifact
    pack: EvidencePack
    validation: ValidationReport
    dataset_id: str
    revision: str
    simulated: bool


class ResearchEngine:
    def __init__(
        self,
        store: SnapshotStore,
        *,
        provider: ResearchProvider | None = None,
        validator: EvidenceValidator | None = None,
    ) -> None:
        self.store = store
        self.retriever = SnapshotEvidenceRetriever(store)
        self.provider = provider or provider_from_name("mock")
        self.validator = validator or EvidenceValidator()

    def analyze(
        self,
        query: ResearchQuery,
        *,
        document_dataset: str,
        document_revision: str,
        financial_dataset: str,
        financial_revision: str,
        output_dataset: str,
        output_revision: str,
        created_at: datetime | None = None,
    ) -> ResearchRunResult:
        if (output_dataset, output_revision) in {
            (document_dataset, document_revision),
            (financial_dataset, financial_revision),
        }:
            raise InvalidInputError("research output must be a new immutable revision.")
        pack = self.retriever.retrieve(
            query,
            document_dataset=document_dataset,
            document_revision=document_revision,
            financial_dataset=financial_dataset,
            financial_revision=financial_revision,
        )
        if len(query.question) > query.max_question_chars:
            raise InvalidInputError("research question exceeds bound.")
        response = self.provider.analyze(query, pack)
        stamp = created_at or datetime.now(UTC)
        artifact_id = _artifact_id(query, pack, self.provider.provider_id)
        draft = ResearchArtifact(
            artifact_id=artifact_id,
            model_id=self.provider.model_id,
            model_version=self.provider.model_version,
            time=TimeIndex(
                event_time=query.research_as_of,
                available_time=query.research_as_of,
                ingested_time=stamp,
            ),
            provenance=Provenance(
                source_kind=SourceKind.SIMULATED,
                dataset_id=output_dataset,
                revision=output_revision,
                vendor="atlas_research_mock",
            ),
            symbols=(normalize_symbol(query.symbol),),
            thesis=response.thesis,
            evidence_refs=response.evidence_refs,
            accession=query.accession,
            claims=response.claims,
            observations=response.observations,
            growth_drivers=response.growth_drivers,
            risks=response.risks,
            bullish_hypotheses=response.bullish_hypotheses,
            bearish_hypotheses=response.bearish_hypotheses,
            counterarguments=response.counterarguments,
            missing_information=response.missing_information,
            uncertainties=response.uncertainties,
            validation_status="unchecked",
            provider_id=self.provider.provider_id,
            simulated=True,
        )
        report = self.validator.validate(draft, pack)
        claims = tuple(
            replace(
                claim,
                citation_mechanically_valid=_claim_ok(claim, report),
                semantically_verified=False,
            )
            for claim in draft.claims
        )
        artifact = replace(
            draft,
            claims=claims,
            validation_status="valid" if report.valid else "invalid",
            validation_errors=tuple(f"{item.code}: {item.message}" for item in report.issues),
        )
        metadata = {
            "provider_id": self.provider.provider_id,
            "model_id": self.provider.model_id,
            "model_version": self.provider.model_version,
            "simulated": True,
            "llm_called": False,
            "untrusted_evidence": True,
            "semantically_verified": False,
            "document_dataset": document_dataset,
            "document_revision": document_revision,
            "financial_dataset": financial_dataset,
            "financial_revision": financial_revision,
            "research_as_of": query.research_as_of.isoformat(),
            "price_disclaimer": (
                "Mock research only. Not LLM analysis, not investment advice, not an order."
            ),
        }
        self.store.create(
            output_dataset,
            output_revision,
            SnapshotPayload(research=(artifact,)),
            source_kind=SourceKind.SIMULATED,
            vendor="atlas_research_mock",
            created_at=stamp,
            metadata=metadata,
        )
        return ResearchRunResult(
            artifact=artifact,
            pack=pack,
            validation=report,
            dataset_id=output_dataset,
            revision=output_revision,
            simulated=True,
        )


def _artifact_id(query: ResearchQuery, pack: EvidencePack, provider_id: str) -> str:
    material = "|".join(
        [
            query.symbol,
            query.accession or "",
            query.research_as_of.isoformat(),
            query.question,
            provider_id,
            *(chunk.chunk_id for chunk in pack.chunks),
            *(fact.ref_id for fact in pack.facts),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"research-{query.symbol}-{provider_id}-{digest}"


def _claim_ok(claim, report: ValidationReport) -> bool:
    return not any(issue.claim_id == claim.claim_id for issue in report.issues)
