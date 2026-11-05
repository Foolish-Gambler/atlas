"""Research provider and publish failures. Never include API keys in messages."""

from __future__ import annotations


class ResearchError(RuntimeError):
    """Base research-pipeline failure."""


class LlmNetworkDisabledError(ResearchError):
    """Raised when an LLM call is attempted without ATLAS_ALLOW_LLM_NETWORK."""


class ResearchBudgetError(ResearchError):
    """Input/output token or request budget exceeded before or during a call."""


class ResearchProviderError(ResearchError):
    """Provider refused, timed out, rate-limited, or returned an unusable payload."""


class ResearchRejectedError(ResearchError):
    """Validated output is unusable and was not published to the research dataset."""
