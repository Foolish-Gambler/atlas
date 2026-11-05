"""OpenAI Responses provider. Offline unless ATLAS_ALLOW_LLM_NETWORK=1."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from atlas.contracts import ResearchClaim
from atlas.research.errors import (
    LlmNetworkDisabledError,
    ResearchBudgetError,
    ResearchProviderError,
)
from atlas.research.models import (
    DEFAULT_MAX_CLAIM_CHARS,
    EvidencePack,
    ModelResearchResponse,
    ResearchQuery,
)
from atlas.research.payload import estimate_tokens, user_message
from atlas.research.prompts import PROMPT_VERSION, SCHEMA_VERSION, SYSTEM_INSTRUCTIONS
from atlas.research.schema import ResearchClaimModel, ResearchOutputModel
from atlas.validation import InvalidInputError

_TRUTHY = frozenset({"1", "true", "yes"})


def llm_network_allowed() -> bool:
    return os.environ.get("ATLAS_ALLOW_LLM_NETWORK", "").strip().lower() in _TRUTHY


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidInputError(f"{name} must be an integer.") from exc


def _env_float(name: str) -> float | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise InvalidInputError(f"{name} must be a number.") from exc


@dataclass(frozen=True, slots=True)
class OpenAIProviderSettings:
    model_id: str
    timeout_seconds: float = 30.0
    max_requests: int = 1
    max_input_tokens: int = 20_000
    max_output_tokens: int = 4_000
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @classmethod
    def from_env(cls, *, model_id: str | None = None) -> OpenAIProviderSettings:
        resolved = model_id or _env("ATLAS_OPENAI_MODEL")
        if not resolved:
            raise InvalidInputError("ATLAS_OPENAI_MODEL is required for --provider openai.")
        return cls(
            model_id=resolved,
            timeout_seconds=float(_env("ATLAS_OPENAI_TIMEOUT") or 30),
            max_requests=_env_int("ATLAS_OPENAI_MAX_REQUESTS", 1),
            max_input_tokens=_env_int("ATLAS_OPENAI_MAX_INPUT_TOKENS", 20_000),
            max_output_tokens=_env_int("ATLAS_OPENAI_MAX_OUTPUT_TOKENS", 4_000),
            input_price_per_million=_env_float("ATLAS_OPENAI_PRICE_INPUT_PER_MILLION"),
            output_price_per_million=_env_float("ATLAS_OPENAI_PRICE_OUTPUT_PER_MILLION"),
        )


class OpenAIResearchProvider:
    """Live OpenAI research. Tests inject a fake client; default is offline."""

    provider_id = "openai"

    def __init__(
        self,
        *,
        settings: OpenAIProviderSettings | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        allow_network: bool | None = None,
        model_id: str | None = None,
    ) -> None:
        self.settings = settings or OpenAIProviderSettings.from_env(model_id=model_id)
        self.model_id = self.settings.model_id
        self.model_version = "openai.responses"
        self._api_key = api_key if api_key is not None else _env("OPENAI_API_KEY")
        self._client = client
        self._allow_network = llm_network_allowed() if allow_network is None else allow_network
        self._requests = 0

    @classmethod
    def from_env(cls, **kwargs: Any) -> OpenAIResearchProvider:
        return cls(**kwargs)

    def analyze(self, query: ResearchQuery, pack: EvidencePack) -> ModelResearchResponse:
        if not self._allow_network:
            raise LlmNetworkDisabledError(
                "OpenAI research is disabled. Set ATLAS_ALLOW_LLM_NETWORK=1 only after "
                "explicit approval. This default does not call the API."
            )
        if not self._api_key:
            raise InvalidInputError("OPENAI_API_KEY is required for --provider openai.")
        if self._requests >= self.settings.max_requests:
            raise ResearchBudgetError("OpenAI request budget already exhausted.")
        user = user_message(query, pack)
        input_tokens = estimate_tokens(SYSTEM_INSTRUCTIONS) + estimate_tokens(user)
        if input_tokens > self.settings.max_input_tokens:
            raise ResearchBudgetError(
                f"estimated input tokens {input_tokens} exceed "
                f"max_input_tokens={self.settings.max_input_tokens}."
            )
        client = self._client or self._build_client()
        self._requests += 1
        try:
            response = client.responses.parse(
                model=self.model_id,
                instructions=SYSTEM_INSTRUCTIONS,
                input=[{"role": "user", "content": user}],
                text_format=ResearchOutputModel,
                store=False,
                max_output_tokens=self.settings.max_output_tokens,
                temperature=0,
                timeout=self.settings.timeout_seconds,
            )
        except Exception as exc:
            raise _map_provider_error(exc) from exc
        parsed = _require_parsed(response)
        usage_in, usage_out = _usage(response)
        cost = _cost(self.settings, usage_in, usage_out)
        return _to_response(
            parsed,
            response_id=getattr(response, "id", None),
            model_version=str(getattr(response, "model", None) or self.model_version),
            input_tokens=usage_in,
            output_tokens=usage_out,
            request_count=self._requests,
            estimated_cost=cost,
        )

    def _build_client(self) -> Any:
        from openai import OpenAI

        return OpenAI(
            api_key=self._api_key,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
        )


def _require_parsed(response: Any) -> ResearchOutputModel:
    status = getattr(response, "status", None)
    if status in {"incomplete", "failed", "cancelled"}:
        raise ResearchProviderError(f"OpenAI response is {status} and cannot be published.")
    refusal = _refusal_text(response)
    if refusal:
        raise ResearchProviderError("OpenAI refused to produce research output.")
    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise ResearchProviderError("OpenAI response did not include structured output.")
    if not isinstance(parsed, ResearchOutputModel):
        parsed = ResearchOutputModel.model_validate(parsed)
    return parsed


def _refusal_text(response: Any) -> str | None:
    for item in getattr(response, "output", ()) or ():
        for content in getattr(item, "content", ()) or ():
            if getattr(content, "type", None) == "refusal":
                return str(getattr(content, "refusal", "refusal"))
            if getattr(content, "refusal", None):
                return str(content.refusal)
    return None


def _usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    return (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )


def _cost(
    settings: OpenAIProviderSettings, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    if settings.input_price_per_million is None or settings.output_price_per_million is None:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    return (
        input_tokens * settings.input_price_per_million
        + output_tokens * settings.output_price_per_million
    ) / 1_000_000


def _to_response(
    parsed: ResearchOutputModel,
    *,
    response_id: str | None,
    model_version: str,
    input_tokens: int | None,
    output_tokens: int | None,
    request_count: int,
    estimated_cost: float | None,
) -> ModelResearchResponse:
    claims = tuple(_claim_from_model(item) for item in parsed.claims)
    refs = list(parsed.evidence_refs)
    for claim in claims:
        refs.extend(claim.evidence_refs)
    return ModelResearchResponse(
        thesis=parsed.thesis[:DEFAULT_MAX_CLAIM_CHARS],
        claims=claims,
        observations=tuple(parsed.observations),
        growth_drivers=tuple(parsed.growth_drivers),
        risks=tuple(parsed.risks),
        bullish_hypotheses=tuple(parsed.bullish_hypotheses),
        bearish_hypotheses=tuple(parsed.bearish_hypotheses),
        counterarguments=tuple(parsed.counterarguments),
        missing_information=tuple(parsed.missing_information),
        uncertainties=tuple(parsed.uncertainties),
        evidence_refs=tuple(dict.fromkeys(refs)),
        llm_called=True,
        simulated=False,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        response_id=response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_count=request_count,
        generated_at=datetime.now(UTC),
        estimated_cost=estimated_cost,
        model_version=model_version,
    )


def _claim_from_model(item: ResearchClaimModel) -> ResearchClaim:
    return ResearchClaim(
        claim_id=item.claim_id,
        text=item.text[:DEFAULT_MAX_CLAIM_CHARS],
        category=item.category,
        evidence_refs=tuple(item.evidence_refs),
        quoted_span=item.quoted_span,
        chunk_id=item.chunk_id,
        fact_id=item.fact_id,
        text_start=item.text_start,
        text_end=item.text_end,
        asserted_value=item.asserted_value,
        asserted_unit=item.asserted_unit,
        asserted_period=item.asserted_period,
        asserted_accession=item.asserted_accession,
        semantically_verified=False,
    )


def _map_provider_error(exc: BaseException) -> ResearchProviderError:
    name = type(exc).__name__
    message = _redact(str(exc) or name)
    if isinstance(exc, ResearchProviderError):
        return exc
    if name in {"APITimeoutError", "TimeoutException", "TimeoutError"}:
        return ResearchProviderError(f"OpenAI request timed out ({name}).")
    if name in {"RateLimitError"} or "429" in message:
        return ResearchProviderError("OpenAI rate limit returned; request was not retried.")
    if name in {"LengthFinishReasonError", "ContentFilterFinishReasonError"}:
        return ResearchProviderError(f"OpenAI response was incomplete ({name}).")
    if name in {"AuthenticationError"}:
        return ResearchProviderError("OpenAI authentication failed.")
    return ResearchProviderError(f"OpenAI request failed ({name}): {message}")


def _redact(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    redacted = text
    if key:
        redacted = redacted.replace(key, "<redacted-openai-key>")
    lowered = redacted
    if "sk-" in lowered:
        parts = []
        for token in redacted.split():
            if token.startswith("sk-"):
                parts.append("<redacted-openai-key>")
            else:
                parts.append(token)
        redacted = " ".join(parts)
    return redacted.replace("Bearer ", "Bearer <redacted> ")
