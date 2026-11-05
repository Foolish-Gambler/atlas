"""Evidence-grounded financial research. OpenAI is opt-in. No orders."""

from atlas.research.engine import ResearchEngine, ResearchRunResult
from atlas.research.models import EvidencePack, ResearchQuery, ValidationReport
from atlas.research.openai_provider import OpenAIResearchProvider
from atlas.research.provider import MockResearchProvider, provider_from_name
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
