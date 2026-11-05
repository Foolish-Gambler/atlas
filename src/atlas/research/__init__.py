"""Evidence-grounded financial research. Mock provider only in M3-A. No orders."""

from atlas.research.engine import ResearchEngine, ResearchRunResult
from atlas.research.models import EvidencePack, ResearchQuery, ValidationReport
from atlas.research.provider import MockResearchProvider, OpenAIResearchProvider, provider_from_name
from atlas.research.retriever import SnapshotEvidenceRetriever
from atlas.research.validator import EvidenceValidator

__all__ = [
    "EvidencePack",
    "EvidenceValidator",
    "MockResearchProvider",
    "OpenAIResearchProvider",
    "ResearchEngine",
    "ResearchQuery",
    "ResearchRunResult",
    "SnapshotEvidenceRetriever",
    "ValidationReport",
    "provider_from_name",
]
